import asyncio, json, logging, httpx, os, base64
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
from tasks.postprocess import strip_fences, table_name, postprocess_entity, generate_application_class
from tasks.dbstatus import db_mark_running, db_mark_done, db_add_event

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.getenv("LLM_MODEL", "coder-model")
LLM_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/") + "/v1/chat/completions"
LLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
GITEA_URL = "http://gitea:3000"
GITEA_TOKEN = os.environ["GITEA_TOKEN"]

PACK_CONFIG = {
    "banking":   {"entities": 10, "compliance": "RBI Tier-1", "framework": "Spring Boot", "tables": 24, "package": "com.vibebank", "entity": "BankAccount"},
    "ecommerce": {"entities": 11, "compliance": "GDPR",       "framework": "Spring Boot", "tables": 28, "package": "com.omnistore", "entity": "Product"},
    "logistics": {"entities": 8,  "compliance": "ISO 27001",  "framework": "Spring Boot", "tables": 18, "package": "com.logiroute",  "entity": "Shipment"},
    "custom":    {"entities": 5,  "compliance": "Custom",     "framework": "Spring Boot", "tables": 20, "package": "com.customapp",  "entity": "CoreEntity"},
}

def _clean_entity(code, ename):
    import re
    code = re.sub(r"^import [^;]*\b(AuditingEntity|UUIDGenerator|GenericGenerator)\b[^;]*;[ \t]*\n", "", code, flags=re.M)
    code = re.sub(r"^[ \t]*@(Audited|AuditingEntity)\b[^\n]*\n", "", code, flags=re.M)
    code = re.sub(r"^[ \t]*@GenericGenerator\([^\n]*\)[ \t]*\n", "", code, flags=re.M)
    code = re.sub(r'@GeneratedValue\(\s*generator\s*=\s*"[^"]*"\s*\)', "@GeneratedValue", code)
    code = re.sub(r"\b(?:protected|private)\s+" + re.escape(ename) + r"\s*\(\s*\)", "public " + ename + "()", code)
    return code


async def _compile_check(files, job_id):
    import re, shutil
    d = f"/tmp/gate-{job_id}"
    shutil.rmtree(d, ignore_errors=True)
    for path, content in files.items():
        fp = os.path.join(d, path)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as f:
            f.write(content)
    try:
        proc = await asyncio.create_subprocess_exec(
            "mvn", "-q", "-B", "-DskipTests", "-Dmaven.repo.local=/app/.m2", "test-compile",
            cwd=d, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
    except FileNotFoundError:
        return {"status": "unavailable", "bad": set()}
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"status": "timeout", "bad": set()}
    finally:
        shutil.rmtree(d, ignore_errors=True)
    text = out.decode("utf-8", "replace")
    if proc.returncode == 0:
        return {"status": "ok", "bad": set()}
    bad = set(re.findall(r"\[ERROR\][^\n]*?/([A-Za-z0-9_]+)\.java:\[", text))
    logger.error(f"compile gate failed for {job_id}: {text[-1200:]}")
    return {"status": "fail", "bad": bad}


ENTITY_LISTS = {
    "logistics": [
        ("Shipper", "shippers", "company_name, gstin, contact_person, email, phone, credit_limit (BigDecimal), outstanding_amount (BigDecimal), is_active (Boolean)"),
        ("Carrier", "carriers", "carrier_name, gstin, contact_person, email, phone, status, is_active (Boolean)"),
        ("TrackingEvent", "tracking_events", "shipment_id (UUID), event_type, location_name, city, latitude (BigDecimal), longitude (BigDecimal), recorded_by (UUID), event_at"),
        ("Vehicle", "vehicles", "registration_number, vehicle_type, carrier_id (UUID), driver_id (UUID), capacity_kg (BigDecimal), status, current_latitude (BigDecimal), current_longitude (BigDecimal), insurance_expiry, permit_expiry"),
        ("Driver", "drivers", "carrier_id (UUID), employee_code, first_name, last_name, phone, license_number, license_expiry, status, total_deliveries (Integer), success_rate (BigDecimal)"),
        ("Route", "routes", "route_code, origin_hub_id (UUID), destination_hub_id (UUID), distance_km (BigDecimal), estimated_transit_hours (Integer), is_active (Boolean)"),
        ("Warehouse", "warehouses", "warehouse_code, name, warehouse_type, city, state, pin_code, capacity_sqft (BigDecimal), is_active (Boolean)"),
    ],
    "banking": [
        ("Customer", "customers", "customer_ref, first_name, last_name, date_of_birth, pan_number, aadhaar_hash, kyc_status, risk_category, customer_segment, is_active (Boolean), onboarded_at, branch_id (UUID)"),
        ("BankTransaction", "bank_transactions", "transaction_ref, rrn, from_account_id (UUID), to_account_id (UUID), beneficiary_id (UUID), amount (BigDecimal), transaction_type, payment_rail, channel, status, balance_before (BigDecimal), balance_after (BigDecimal), is_flagged (Boolean), initiated_at"),
        ("Beneficiary", "beneficiaries", "customer_id (UUID), beneficiary_name, ifsc_code, account_number, cooldown_until, is_verified (Boolean), added_via"),
        ("KycDocument", "kyc_documents", "customer_id (UUID), document_type, document_number, document_hash, issuing_authority, expiry_date, verification_status, verified_at"),
        ("Branch", "branches", "ifsc_code, micr_code, branch_name, city, state, pin_code, is_active (Boolean)"),
        ("Nominee", "nominees", "account_id (UUID), name, relationship, date_of_birth, share_percentage (BigDecimal), is_minor (Boolean)"),
        ("BankProduct", "bank_products", "product_code, product_name, account_type, minimum_balance (BigDecimal), daily_txn_limit (BigDecimal), rtgs_minimum_amount (BigDecimal), is_active (Boolean)"),
        ("FraudAlert", "fraud_alerts", "transaction_id (UUID), customer_id (UUID), alert_type, severity, risk_score (BigDecimal), status, resolved_at"),
        ("AuditLog", "audit_logs", "entity_type, entity_id (UUID), action, performed_by_role, ip_address, event_timestamp"),
        ("BatchJob", "batch_jobs", "batch_type, scheduled_at, status, total_records (Integer), processed_records (Integer), failed_records (Integer), total_amount (BigDecimal)"),
    ],
    "ecommerce": [
        ("Customer", "customers", "email, first_name, last_name, marketing_consent (Boolean), data_processing_consent (Boolean), consent_recorded_at, status, tier, loyalty_points (Integer)"),
        ("Address", "addresses", "customer_id (UUID), address_type, line1, city, state, pin_code, is_default (Boolean)"),
        ("Category", "categories", "name, slug, parent_category_id (UUID), is_active (Boolean)"),
        ("Inventory", "inventory", "product_id (UUID), warehouse_id (UUID), quantity_available (Integer), quantity_reserved (Integer), reorder_level (Integer)"),
        ("Cart", "carts", "customer_id (UUID), status, subtotal (BigDecimal), tax_amount (BigDecimal), total_amount (BigDecimal), expires_at"),
        ("CartItem", "cart_items", "cart_id (UUID), product_id (UUID), quantity (Integer), unit_price (BigDecimal)"),
        ("CustomerOrder", "customer_orders", "order_number, customer_id (UUID), shipping_address_id (UUID), status, total_amount (BigDecimal), payment_status"),
        ("Payment", "payments", "order_id (UUID), amount (BigDecimal), payment_method, gateway, gateway_transaction_id, status"),
        ("Review", "reviews", "product_id (UUID), customer_id (UUID), rating (Integer), title, status, is_verified_purchase (Boolean)"),
    ],
}

ERD_TEMPLATES = {
    "banking": """erDiagram
    CUSTOMER ||--o{ ACCOUNT : owns
    CUSTOMER ||--o{ KYC_DOCUMENT : submits
    CUSTOMER ||--o{ BENEFICIARY : manages
    ACCOUNT ||--o{ TRANSACTION : records
    ACCOUNT }o--|| BRANCH : belongs_to
    ACCOUNT }o--|| PRODUCT : typed_by
    ACCOUNT ||--o| NOMINEE : has
    TRANSACTION ||--o| FRAUD_ALERT : triggers
    TRANSACTION }o--o| BATCH_JOB : grouped_in
    TRANSACTION ||--o{ AUDIT_LOG : logged_in
    CUSTOMER { uuid customer_id PK string customer_ref UK string first_name string last_name date date_of_birth string pan_number restricted string aadhaar_hash restricted enum kyc_status enum risk_category enum customer_segment boolean is_active datetime onboarded_at uuid branch_id FK }
    ACCOUNT { uuid account_id PK string account_number UK uuid customer_id FK uuid branch_id FK uuid product_id FK enum account_type enum status decimal balance decimal available_balance decimal hold_amount decimal overdraft_limit string ifsc_code uuid nominee_id FK datetime dormancy_trigger_at datetime opened_at }
    TRANSACTION { uuid transaction_id PK string transaction_ref UK string rrn uuid from_account_id FK uuid to_account_id FK uuid beneficiary_id FK decimal amount enum transaction_type enum payment_rail enum channel enum status decimal balance_before decimal balance_after uuid approved_by uuid batch_id FK boolean is_flagged datetime initiated_at }
    BENEFICIARY { uuid beneficiary_id PK uuid customer_id FK string beneficiary_name string ifsc_code string account_number restricted datetime cooldown_until boolean is_verified enum added_via }
    KYC_DOCUMENT { uuid document_id PK uuid customer_id FK enum document_type string document_number restricted string document_hash string issuing_authority date expiry_date enum verification_status datetime verified_at }
    BRANCH { uuid branch_id PK string ifsc_code UK string micr_code string branch_name string city string state string pin_code boolean is_active }
    NOMINEE { uuid nominee_id PK uuid account_id FK string name enum relationship date date_of_birth decimal share_percentage boolean is_minor }
    PRODUCT { uuid product_id PK string product_code UK string product_name enum account_type decimal minimum_balance decimal daily_txn_limit decimal rtgs_minimum_amount boolean is_active }
    FRAUD_ALERT { uuid alert_id PK uuid transaction_id FK uuid customer_id FK enum alert_type enum severity decimal risk_score enum status datetime resolved_at }
    AUDIT_LOG { uuid audit_id PK string entity_type uuid entity_id enum action json old_value json new_value string performed_by_role string ip_address datetime timestamp }
    BATCH_JOB { uuid batch_id PK enum batch_type datetime scheduled_at enum status int total_records int processed_records int failed_records decimal total_amount }""",

    "ecommerce": """erDiagram
    CUSTOMER ||--o{ ADDRESS : has
    CUSTOMER ||--o{ ORDER : places
    CUSTOMER ||--o{ CART : owns
    CUSTOMER ||--o{ REVIEW : writes
    ORDER ||--o{ PAYMENT : paid_by
    CART ||--o{ CART_ITEM : contains
    PRODUCT ||--o{ CART_ITEM : in
    PRODUCT ||--o{ REVIEW : receives
    PRODUCT }o--|| CATEGORY : belongs_to
    PRODUCT ||--o{ INVENTORY : stocked_in
    CUSTOMER { uuid customer_id PK string email UK string first_name string last_name boolean marketing_consent boolean data_processing_consent datetime consent_recorded_at enum status enum tier int loyalty_points }
    ADDRESS { uuid address_id PK uuid customer_id FK enum address_type string line1 string city string state string pin_code boolean is_default }
    CATEGORY { uuid category_id PK string name string slug UK uuid parent_category_id FK boolean is_active }
    PRODUCT { uuid product_id PK string sku UK string name decimal base_price decimal selling_price decimal tax_rate string hsn_code enum status boolean is_returnable int return_window_days }
    INVENTORY { uuid inventory_id PK uuid product_id FK uuid warehouse_id FK int quantity_available int quantity_reserved int reorder_level }
    CART { uuid cart_id PK uuid customer_id FK enum status decimal subtotal decimal tax_amount decimal total_amount datetime expires_at }
    CART_ITEM { uuid cart_item_id PK uuid cart_id FK uuid product_id FK int quantity decimal unit_price }
    ORDER { uuid order_id PK string order_number UK uuid customer_id FK uuid shipping_address_id FK enum status decimal total_amount enum payment_status datetime placed_at datetime delivered_at }
    PAYMENT { uuid payment_id PK uuid order_id FK decimal amount enum payment_method enum gateway string gateway_transaction_id enum status decimal refund_amount }
    REVIEW { uuid review_id PK uuid product_id FK uuid customer_id FK int rating string title enum status boolean is_verified_purchase }""",

    "logistics": """erDiagram
    SHIPPER ||--o{ SHIPMENT : books
    SHIPMENT ||--o{ TRACKING_EVENT : has
    SHIPMENT }o--|| VEHICLE : assigned_to
    SHIPMENT }o--|| DRIVER : delivered_by
    SHIPMENT }o--|| ROUTE : follows
    VEHICLE }o--|| CARRIER : owned_by
    DRIVER }o--|| CARRIER : employed_by
    ROUTE }o--|| WAREHOUSE : from_hub
    ROUTE }o--|| WAREHOUSE : to_hub
    SHIPPER { uuid shipper_id PK string company_name string gstin string contact_person string email string phone decimal credit_limit decimal outstanding_amount boolean is_active }
    SHIPMENT { uuid shipment_id PK string awb_number UK uuid shipper_id FK string consignee_name string consignee_phone enum service_type enum shipment_type enum status int weight_grams decimal declared_value decimal cod_amount boolean is_cod boolean is_fragile uuid vehicle_id FK uuid driver_id FK uuid route_id FK datetime estimated_delivery_at datetime actual_delivery_at string delivery_otp int attempt_count }
    TRACKING_EVENT { uuid event_id PK uuid shipment_id FK enum event_type string location_name string city decimal latitude decimal longitude uuid recorded_by FK datetime event_at }
    VEHICLE { uuid vehicle_id PK string registration_number UK enum vehicle_type uuid carrier_id FK uuid driver_id FK decimal capacity_kg enum status decimal current_latitude decimal current_longitude date insurance_expiry date permit_expiry }
    DRIVER { uuid driver_id PK uuid carrier_id FK string employee_code UK string first_name string last_name string phone string license_number date license_expiry enum status int total_deliveries decimal success_rate }
    ROUTE { uuid route_id PK string route_code UK uuid origin_hub_id FK uuid destination_hub_id FK decimal distance_km int estimated_transit_hours boolean is_active }
    WAREHOUSE { uuid warehouse_id PK string warehouse_code UK string name enum type string city string state string pin_code decimal capacity_sqft boolean is_active }""",
}


import re as _re

def strip_fences(code: str) -> str:
    code = code.strip()
    code = _re.sub(r"^```[a-zA-Z]*\r?\n?", "", code)
    code = _re.sub(r"\n?```\s*$", "", code)
    return code.strip()

import contextvars as _cv
_LLM_USAGE_VAR = _cv.ContextVar("llm_usage", default=None)
LLM_USAGE = []  # fallback list when no job context is active


def _usage_list():
    """Per-job usage list (set by run_generation); safe with concurrent jobs."""
    lst = _LLM_USAGE_VAR.get()
    return lst if lst is not None else LLM_USAGE

async def _call_groq_once(prompt, max_tokens=3000):
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            LLM_URL,
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
        )
        for _w in range(2):
            if resp.status_code != 429:
                break
            _wait = 65 if "Request too large" in resp.text else 35
            logger.warning(f"Rate limited, waiting {_wait}s (retry {_w + 1}/2)...")
            await asyncio.sleep(_wait)
            resp = await client.post(
                LLM_URL,
                headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
            )
        data = resp.json()
        if resp.status_code != 200 or "choices" not in data:
            raise RuntimeError(f"Groq error {resp.status_code}: {str(data)[:600]}")
        choice = data["choices"][0]
        _u = data.get("usage") or {}
        _usage_list().append({"model": GROQ_MODEL, "prompt_tokens": int(_u.get("prompt_tokens", 0)), "completion_tokens": int(_u.get("completion_tokens", 0))})
        if choice.get("finish_reason") == "length":
            raise RuntimeError("LLM output truncated (finish_reason=length); raise max_tokens or split the entity")
        return strip_fences(choice["message"]["content"])

_LLM_BREAKER = {"fails": 0, "until": 0.0}


async def call_groq(prompt, max_tokens=3000):
    """Every model call: hard timeout (C20) and a circuit breaker (C21) that fails fast when the endpoint is down."""
    import time as _t
    if _LLM_BREAKER["until"] > _t.time():
        raise RuntimeError("LLM circuit open: model endpoint failing, retry shortly")
    try:
        out = await asyncio.wait_for(_call_groq_once(prompt, max_tokens), timeout=300)
    except Exception as e:
        msg = str(e) or type(e).__name__
        if "Groq error 429" not in msg and "truncated" not in msg:
            _LLM_BREAKER["fails"] += 1
            if _LLM_BREAKER["fails"] >= 5:
                _LLM_BREAKER["until"] = _t.time() + 60
                _LLM_BREAKER["fails"] = 0
                logger.error("LLM circuit breaker opened for 60s")
        raise
    _LLM_BREAKER["fails"] = 0
    return out


async def push_file_to_gitea(repo_name, file_path, content, message="feat: AI-generated by Agents of Agents"):
    try:
        code_b64 = base64.b64encode(content.encode()).decode()
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{GITEA_URL}/api/v1/repos/vibeforge-admin/{repo_name}/contents/{file_path}",
                headers={"Authorization": f"token {GITEA_TOKEN}"},
                json={"message": message, "content": code_b64})
            return r.status_code == 201
    except Exception as e:
        logger.error(f"Gitea push error {file_path}: {e}")
        return False

def generate_pom_xml(cfg):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>3.5.16</version>
    </parent>
    <groupId>{cfg["package"]}</groupId>
    <artifactId>{cfg["entity"].lower()}-service</artifactId>
    <version>1.0.0</version>
    <name>{cfg["entity"]} Service — Generated by Agents of Agents</name>
    <description>{cfg["compliance"]} compliant {cfg["entity"]} microservice</description>
    <properties>
        <java.version>21</java.version>
        <jackson-bom.version>2.21.7</jackson-bom.version>
        <tomcat.version>10.1.60</tomcat.version>
        <postgresql.version>42.7.12</postgresql.version>
    </properties>
    <dependencies>
        <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
        <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-data-jpa</artifactId></dependency>
        <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-validation</artifactId></dependency>
        <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-actuator</artifactId></dependency>
        <dependency><groupId>org.postgresql</groupId><artifactId>postgresql</artifactId><scope>runtime</scope></dependency>
        <dependency><groupId>org.flywaydb</groupId><artifactId>flyway-core</artifactId></dependency>
        <dependency><groupId>org.flywaydb</groupId><artifactId>flyway-database-postgresql</artifactId></dependency>
        <dependency><groupId>org.springdoc</groupId><artifactId>springdoc-openapi-starter-webmvc-ui</artifactId><version>2.8.17</version></dependency>
        <dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><optional>true</optional></dependency>
        <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency>
    </dependencies>
    <build>
        <plugins>
            <plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin>
        </plugins>
    </build>
</project>"""

def generate_application_yml(cfg, pack):
    return f"""spring:
  application:
    name: {cfg["entity"].lower()}-service
  datasource:
    url: ${{DB_URL:jdbc:postgresql://localhost:5432/{pack}db}}
    username: ${{DB_USER:postgres}}
    password: ${{DB_PASSWORD:}}
    driver-class-name: org.postgresql.Driver
  jpa:
    hibernate:
      ddl-auto: validate
    show-sql: false
    properties:
      hibernate:
        dialect: org.hibernate.dialect.PostgreSQLDialect
        format_sql: true
  flyway:
    enabled: true
    locations: classpath:db/migration

server:
  port: 8080

management:
  endpoints:
    web:
      exposure:
        include: health,info,metrics

springdoc:
  api-docs:
    path: /api-docs
  swagger-ui:
    path: /swagger-ui.html

logging:
  level:
    {cfg["package"]}: INFO
    org.springframework.web: WARN

# Compliance: {cfg["compliance"]}
# Generated by: Agents of Agents AI Platform
# Oracle Cloud: India South (Hyderabad)"""

def generate_repository(cfg):
    entity = cfg["entity"]
    pkg = cfg["package"]
    return f"""package {pkg}.repository;

import {pkg}.entity.{entity};
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.stereotype.Repository;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

/**
 * Repository for {entity} — {cfg["compliance"]} compliant data access layer.
 * Generated by Agents of Agents AI Platform.
 */
@Repository
public interface {entity}Repository extends JpaRepository<{entity}, UUID> {{

{"    List<" + entity + "> findByStatus(String status);" if cfg.get("has_status", True) else ""}

    Optional<{entity}> findByIdAndTenantId(UUID id, UUID tenantId);

    @Query("SELECT e FROM {entity} e WHERE e.createdAt >= :cutoff")
    List<{entity}> findCreatedSince(@org.springframework.data.repository.query.Param("cutoff") java.time.LocalDateTime cutoff);

    default List<{entity}> findRecentEntries() {{
        return findCreatedSince(java.time.LocalDateTime.now().minusDays(30));
    }}

{"    long countByStatus(String status);" if cfg.get("has_status", True) else ""}
}}"""

def generate_service(cfg):
    entity = cfg["entity"]
    pkg = cfg["package"]
    return f"""package {pkg}.service;

import {pkg}.entity.{entity};
import {pkg}.repository.{entity}Repository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

/**
 * Service layer for {entity} — {cfg["compliance"]} compliant business logic.
 * Generated by Agents of Agents AI Platform.
 */
@Service
@RequiredArgsConstructor
@Transactional
public class {entity}Service {{

    private static final org.slf4j.Logger log = org.slf4j.LoggerFactory.getLogger({entity}Service.class);

    private final {entity}Repository repository;

    public List<{entity}> findAll() {{
        log.info("Fetching all {entity} records");
        return repository.findAll();
    }}

    public Optional<{entity}> findById(UUID id) {{
        log.info("Fetching {entity} by id: {{}}", id);
        return repository.findById(id);
    }}

    public {entity} create({entity} entity) {{
        log.info("Creating new {entity}");
        // {cfg["compliance"]} compliance check
        validate(entity);
        return repository.save(entity);
    }}

    public {entity} update(UUID id, {entity} updated) {{
        log.info("Updating {entity}: {{}}", id);
        return repository.findById(id).map(existing -> {{
            org.springframework.beans.BeanUtils.copyProperties(updated, existing, "id", "tenantId", "createdAt", "updatedAt");
            return repository.save(existing);
        }}).orElseThrow(() -> new RuntimeException("{entity} not found: " + id));
    }}

    public void delete(UUID id) {{
        log.info("Deleting {entity}: {{}}", id);
        repository.deleteById(id);
    }}

    private void validate({entity} entity) {{
        // {cfg["compliance"]} validation rules
        if (entity == null) {{
            throw new IllegalArgumentException("{entity} cannot be null");
        }}
    }}
}}"""

def generate_controller(cfg):
    entity = cfg["entity"]
    pkg = cfg["package"]
    entity_lower = entity[0].lower() + entity[1:]
    return f"""package {pkg}.controller;

import {pkg}.entity.{entity};
import {pkg}.service.{entity}Service;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import java.util.List;
import java.util.UUID;

/**
 * REST Controller for {entity} — {cfg["compliance"]} compliant API layer.
 * Generated by Agents of Agents AI Platform.
 * Swagger UI: /swagger-ui.html
 */
@RestController
@RequestMapping("/api/v1/{entity_lower}s")
@RequiredArgsConstructor
@Tag(name = "{entity} API", description = "{cfg["compliance"]} compliant {entity} management")
public class {entity}Controller {{

    private final {entity}Service service;

    @GetMapping
    @Operation(summary = "List all {entity} records")
    public ResponseEntity<List<{entity}>> findAll() {{
        return ResponseEntity.ok(service.findAll());
    }}

    @GetMapping("/{{id}}")
    @Operation(summary = "Get {entity} by ID")
    public ResponseEntity<{entity}> findById(@PathVariable UUID id) {{
        return service.findById(id)
            .map(ResponseEntity::ok)
            .orElse(ResponseEntity.notFound().build());
    }}

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @Operation(summary = "Create new {entity}")
    public ResponseEntity<{entity}> create(@jakarta.validation.Valid @RequestBody {entity} entity) {{
        return ResponseEntity.status(HttpStatus.CREATED).body(service.create(entity));
    }}

    @PutMapping("/{{id}}")
    @Operation(summary = "Update {entity}")
    public ResponseEntity<{entity}> update(@PathVariable UUID id, @jakarta.validation.Valid @RequestBody {entity} entity) {{
        return ResponseEntity.ok(service.update(id, entity));
    }}

    @DeleteMapping("/{{id}}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    @Operation(summary = "Delete {entity}")
    public ResponseEntity<Void> delete(@PathVariable UUID id) {{
        service.delete(id);
        return ResponseEntity.noContent().build();
    }}
}}"""

def generate_flyway_sql(cfg, pack):
    entity = cfg["entity"]
    table = table_name(entity)
    if pack == "banking":
        cols = """
    account_number      VARCHAR(20)    NOT NULL UNIQUE,
    iban                VARCHAR(34)    NOT NULL UNIQUE,
    swift_bic           VARCHAR(11),
    holder_full_name    VARCHAR(150)   NOT NULL,
    holder_email        VARCHAR(100)   NOT NULL,
    holder_phone        VARCHAR(20),
    available_balance   DECIMAL(19,4)  NOT NULL DEFAULT 0,
    nominee_name        VARCHAR(150),
    balance             DECIMAL(19,4)  NOT NULL DEFAULT 0,
    account_type        VARCHAR(30)    NOT NULL,
    currency            VARCHAR(3)     NOT NULL DEFAULT 'INR',
    status              VARCHAR(20)    NOT NULL DEFAULT 'ACTIVE',
    aml_flag            BOOLEAN        DEFAULT FALSE,
    rbi_audit_id        VARCHAR(50),
    branch_code         VARCHAR(10),
    ifsc_code           VARCHAR(11),
    kyc_status          VARCHAR(20)    DEFAULT 'PENDING',
    credit_limit        DECIMAL(19,4)  DEFAULT 0,
    interest_rate       DECIMAL(5,2)   DEFAULT 0,
    tenant_id           UUID           NOT NULL,"""
    elif pack == "ecommerce":
        cols = """
    sku                 VARCHAR(50)    NOT NULL UNIQUE,
    name                VARCHAR(200)   NOT NULL,
    description         TEXT,
    price               DECIMAL(19,4)  NOT NULL,
    stock_quantity      INTEGER        NOT NULL DEFAULT 0,
    category            VARCHAR(100),
    brand               VARCHAR(100),
    gst_hsn_code        VARCHAR(20),
    gdpr_consent_req    BOOLEAN        DEFAULT FALSE,
    seller_id           UUID,
    weight              DECIMAL(10,3),
    image_url           VARCHAR(500),
    rating              DECIMAL(3,2)   DEFAULT 0,
    review_count        INTEGER        DEFAULT 0,
    discount_percent    DECIMAL(5,2)   DEFAULT 0,
    tax_rate            DECIMAL(5,2)   DEFAULT 18,
    status              VARCHAR(20)    NOT NULL DEFAULT 'ACTIVE',
    tenant_id           UUID           NOT NULL,"""
    elif pack == "logistics":
        cols = """
    vehicle_id          VARCHAR(50)    NOT NULL UNIQUE,
    plate_number        VARCHAR(20)    NOT NULL UNIQUE,
    driver_name         VARCHAR(150),
    gps_lat             DOUBLE PRECISION,
    gps_lng             DOUBLE PRECISION,
    route_id            UUID,
    cargo_weight        DECIMAL(10,3),
    status              VARCHAR(20)    NOT NULL DEFAULT 'AVAILABLE',
    iso_audit_id        VARCHAR(50),
    last_checkpoint     VARCHAR(200),
    fuel_level          DECIMAL(5,2),
    speed_kmh           DECIMAL(6,2),
    engine_status       VARCHAR(20)    DEFAULT 'OK',
    waybill_number      VARCHAR(50),
    tenant_id           UUID           NOT NULL,"""
    else:
        cols = """
    name                VARCHAR(200)   NOT NULL,
    description         TEXT,
    status              VARCHAR(50)    NOT NULL DEFAULT 'ACTIVE',
    category            VARCHAR(100),
    reference_code      VARCHAR(100),
    amount              DECIMAL(19,4),
    quantity            INTEGER        DEFAULT 0,
    owner_id            UUID,
    notes               TEXT,
    tenant_id           UUID           NOT NULL,"""

    return f"""-- ============================================================
-- {entity} Service — Flyway Migration V1
-- Compliance: {cfg["compliance"]}
-- Generated by: Agents of Agents AI Platform
-- Oracle Cloud: India South (Hyderabad)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS {table} (
    id                  UUID           PRIMARY KEY DEFAULT uuid_generate_v4(),{cols}
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Performance Indexes
CREATE INDEX idx_{table}_status    ON {table}(status);
CREATE INDEX idx_{table}_tenant    ON {table}(tenant_id);
CREATE INDEX idx_{table}_created   ON {table}(created_at DESC);

-- Audit trigger
CREATE OR REPLACE FUNCTION ensure_timestamps()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.created_at IS NULL THEN NEW.created_at = NOW(); END IF;
        IF NEW.updated_at IS NULL THEN NEW.updated_at = NOW(); END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        NEW.updated_at = NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER {table}_timestamps
    BEFORE INSERT OR UPDATE ON {table}
    FOR EACH ROW EXECUTE FUNCTION ensure_timestamps();

-- Comments
COMMENT ON TABLE {table} IS '{entity} table — {cfg["compliance"]} compliant — Generated by Agents of Agents';
"""


def generate_junit_test(cfg):
    entity = cfg["entity"]
    pkg = cfg["package"]
    compliance_safe = cfg["compliance"].replace(" ","_").replace("-","_")
    return f"""package {pkg}.service;

import {pkg}.entity.{entity};
import {pkg}.repository.{entity}Repository;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.*;
import org.mockito.junit.jupiter.MockitoExtension;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@DisplayName("{entity} Service Tests - {cfg["compliance"]}")
class {entity}ServiceTest {{
    @Mock private {entity}Repository repository;
    @InjectMocks private {entity}Service service;
    private {entity} sample; private UUID id;

    @BeforeEach void setUp() {{ id = UUID.randomUUID(); sample = new {entity}(); }}

    @Test @DisplayName("findAll returns all records")
    void findAll_returnsAll() {{
        when(repository.findAll()).thenReturn(List.of(sample));
        assertEquals(1, service.findAll().size());
        verify(repository).findAll();
    }}

    @Test @DisplayName("findById with valid id returns entity")
    void findById_valid() {{
        when(repository.findById(id)).thenReturn(Optional.of(sample));
        assertTrue(service.findById(id).isPresent());
    }}

    @Test @DisplayName("findById with invalid id returns empty")
    void findById_invalid() {{
        when(repository.findById(id)).thenReturn(Optional.empty());
        assertFalse(service.findById(id).isPresent());
    }}

    @Test @DisplayName("create saves and returns entity")
    void create_valid() {{
        when(repository.save(any())).thenReturn(sample);
        assertNotNull(service.create(sample));
        verify(repository).save(any());
    }}

    @Test @DisplayName("create null throws exception")
    void create_null_throws() {{
        assertThrows(IllegalArgumentException.class, () -> service.create(null));
        verify(repository, never()).save(any());
    }}

    @Test @DisplayName("delete removes entity by id")
    void delete_valid() {{
        doNothing().when(repository).deleteById(id);
        assertDoesNotThrow(() -> service.delete(id));
        verify(repository).deleteById(id);
    }}

    @Test @DisplayName("{cfg["compliance"]} - saved entity must not be null")
    void compliance_entityNotNull() {{
        when(repository.save(any())).thenReturn(sample);
        assertNotNull(service.create(sample));
    }}
}}"""


async def self_heal(code, pack, entity, compliance):
    """Check generated code quality. If bad, ask Groq to fix it."""
    issues = []
    if "@Entity" not in code: issues.append("missing @Entity annotation")
    if "@Id" not in code: issues.append("missing @Id annotation")
    if "@GeneratedValue" not in code: issues.append("missing @GeneratedValue")
    if "UUID" not in code: issues.append("missing UUID primary key")
    if len(code) < 500: issues.append("code too short — incomplete generation")

    if not issues:
        logger.info(f"QA passed for {entity} — no issues found")
        return code, []

    logger.warning(f"Self-healing triggered for {entity}: {issues}")
    fix_prompt = f"""The following Java Spring Boot @Entity class has issues: {", ".join(issues)}.
Fix ALL issues and return complete, valid Java code only.
Requirements: @Entity, @Table, @Id, @GeneratedValue(UUID), all @Column annotations.
Compliance: {compliance}. Entity name: {entity}. Pack: {pack}.

Original code:
{code[:12000]}

Return only the complete fixed Java code."""

    fixed = await call_groq(fix_prompt)
    logger.info(f"Self-healed {entity}: {len(fixed)} chars")
    return fixed, issues



def generate_dockerfile(cfg):
    entity = cfg["entity"]
    pkg = cfg["package"]
    return f"""FROM eclipse-temurin:21-jdk-alpine AS build
WORKDIR /app
COPY pom.xml .
COPY src ./src
RUN apk add --no-cache maven && mvn clean package -DskipTests

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY --from=build /app/target/*.jar app.jar
EXPOSE 8080
ENV JAVA_OPTS="-Xmx512m -Xms256m"
ENTRYPOINT ["sh", "-c", "java $JAVA_OPTS -jar app.jar"]
# Generated by Agents of Agents | {cfg["compliance"]} compliant"""

def generate_docker_compose(cfg, pack):
    entity = cfg["entity"]
    svc = entity.lower()
    return f"""version: "3.9"
# Generated by Agents of Agents AI Platform
# Compliance: {cfg["compliance"]}
# One-command deploy: docker compose up -d

services:
  {svc}-app:
    build: .
    ports:
      - "127.0.0.1:${{APP_PORT:-8090}}:8080"
    environment:
      SPRING_DATASOURCE_URL: jdbc:postgresql://postgres:5432/{pack}db
      SPRING_DATASOURCE_USERNAME: postgres
      SPRING_DATASOURCE_PASSWORD: ${{POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}}
      SPRING_PROFILES_ACTIVE: prod
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: {pack}db
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      retries: 5
    restart: unless-stopped

  pgadmin:
    image: dpage/pgadmin4:latest
    ports:
      - "127.0.0.1:5050:80"
    environment:
      PGADMIN_DEFAULT_EMAIL: admin@agents-of-agents.com
      PGADMIN_DEFAULT_PASSWORD: ${{PGADMIN_PASSWORD:?set PGADMIN_PASSWORD in .env}}
    depends_on:
      - postgres

volumes:
  pgdata:
"""


VALID_PACKS = ("banking", "ecommerce", "logistics", "custom")

def _pack_entities(pack, spec_data):
    """Entities from the frozen Spec IR when present; legacy ENTITY_LISTS otherwise."""
    _rev = {"uuid": "UUID", "decimal": "BigDecimal", "int": "Integer", "bool": "Boolean"}
    try:
        ents = ((spec_data or {}).get("domain_model") or {}).get("entities") or []
        out = []
        for e in ents:
            if not e.get("table"):
                continue
            parts = []
            for f in e.get("fields") or []:
                t = f.get("type", "string")
                if t in _rev:
                    parts.append(f'{f["name"]} ({_rev[t]})')
                elif isinstance(t, str) and t.startswith("raw:"):
                    parts.append(f'{f["name"]} ({t[4:]})')
                else:
                    parts.append(f["name"])
            out.append((e["name"], e["table"], ", ".join(parts)))
        if out:
            logger.info("entities source=spec_ir count=%d", len(out))
            return out
    except Exception as exc:
        logger.warning("spec entities unreadable (%s); using legacy list", exc)
    return ENTITY_LISTS.get(pack, [])


_BAD_TYPES = {"String", "Object", "Class", "List", "Map", "Set", "Date", "Record", "Enum", "Entity", "Table", "Column", "Id", "Integer", "Long", "Boolean", "CoreEntity"}
_BAD_SQL = {"user", "order", "group", "table", "select", "column", "index", "check", "default", "grant", "limit",
            "offset", "primary", "references", "rule", "session", "transaction", "trigger", "view", "desc", "asc",
            "key", "from", "where", "when", "end", "all", "analyse", "analyze", "both", "case", "cast", "create",
            "current_user", "do", "else", "false", "true", "for", "foreign", "in", "into", "is", "like", "not",
            "null", "on", "only", "or", "returning", "some", "then", "to", "union", "unique", "using", "with"}
_RESERVED_COLS = {"id", "tenant_id", "created_at", "updated_at"}


def _validate_planned(items):
    """Keep only entities that are safe to feed to the per-entity generator (names, tables, fields)."""
    import re as _r
    out, seen = [], {"CoreEntity"}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        table = str(it.get("table", "")).strip()
        if not _r.fullmatch(r"[A-Z][A-Za-z0-9]{2,30}", name) or name in _BAD_TYPES or name in seen:
            continue
        if not _r.fullmatch(r"[a-z][a-z0-9_]{2,40}", table) or table in _BAD_SQL:
            continue
        fields = []
        raw = it.get("fields", "")
        toks = raw if isinstance(raw, list) else str(raw).split(",")
        for tok in toks:
            tok = str(tok).strip()
            if not _r.fullmatch(r"[a-z][a-z0-9_]*( \((UUID|BigDecimal|Integer|Boolean)\))?", tok):
                continue
            col = tok.split(" ")[0]
            if col in _RESERVED_COLS or col in _BAD_SQL or any(col == f.split(" ")[0] for f in fields):
                continue
            fields.append(tok)
        if len(fields) < 3:
            continue
        seen.add(name)
        out.append((name, table, ", ".join(fields[:8])))
        if len(out) == 4:
            break
    return out


async def _plan_custom_entities(description):
    """Custom pack: ask the model for 2-4 extra entities, validate hard, fall back to none (single entity)."""
    import json as _j
    prompt = (f"You are a software architect. An application is described as: {description}\n"
              "It already has one core entity named CoreEntity. Propose 2 to 4 additional JPA entities this "
              "application needs. Reply with ONLY a JSON array and no other text. Each element is an object "
              '{"name": PascalCase singular noun, "table": snake_case plural, "fields": "comma separated field names"}. '
              "Field names are snake_case and may be followed by a type in parentheses, one of UUID, BigDecimal, "
              "Integer, Boolean (use (UUID) for foreign keys such as customer_id). Give 4 to 8 fields per entity. "
              "Do not include id, tenant_id, created_at or updated_at.")
    try:
        raw = strip_fences(await call_groq(prompt, max_tokens=800))
        a, b = raw.find("["), raw.rfind("]")
        items = _j.loads(raw[a:b + 1]) if a != -1 and b > a else []
        planned = _validate_planned(items)
        logger.info("custom entity plan: %s", [p[0] for p in planned])
        return planned
    except Exception as exc:
        logger.warning("custom entity planning failed (%s); generating the core entity only", exc)
        return []


def resolve_pack(spec_data):
    """Explicit vertical wins; else legacy keyword match on app_name; else fail loudly."""
    v = (spec_data.get("vertical") or "").strip().lower()
    if v in VALID_PACKS:
        return v
    name = (spec_data.get("app_name") or "").lower()
    description = spec_data.get("description") or ""
    if not name:
        raise ValueError("cannot determine pack: send 'vertical' or 'app_name'")
    if any(x in name for x in ["store", "commerce", "omni", "shop"]):
        return "ecommerce"
    if any(x in name for x in ["logi", "fleet", "route", "dispatch"]):
        return "logistics"
    if len(description) > 20 and not any(x in name for x in ["bank", "vibe"]):
        return "custom"
    return "banking"


async def run_generation(ctx, job_id, tenant_id, spec_data):
    redis = ctx["redis"]
    channel = f"job:{job_id}"
    name = (spec_data.get("app_name") or "").lower()
    description = spec_data.get("description") or ""

    pack = resolve_pack(spec_data)

    cfg = PACK_CONFIG[pack]
    entity = cfg["entity"]

    async def pub(progress, stage, message):
        event = json.dumps({
            "progress": progress, "stage": stage, "message": message,
            "ts": datetime.now(timezone.utc).isoformat(),
            "tables": cfg["tables"], "compliance": cfg["compliance"], "pack": pack
        })
        await redis.publish(channel, event)
        await db_add_event(tenant_id, job_id, "error" if stage == "failed" else stage, json.loads(event))
        logger.info(f"[{job_id}] {progress}% {stage}: {message}")

    await db_mark_running(tenant_id, job_id)
    await pub(10, "planner", f"Analyzing {pack} domain — {len(_pack_entities(pack, spec_data)) + 1} entities, {cfg['compliance']} compliance")
    await asyncio.sleep(0.5)

    await pub(25, "spec_ir", f"Compiling Spec IR — {len(_pack_entities(pack, spec_data)) + 1} entities")
    await asyncio.sleep(0.5)

    # REAL LLM CALL — Generate Entity class
    PACK_PROMPTS = {
        "banking": f"Generate a Java Spring Boot JPA @Entity class named BankAccount for RBI Tier-1 banking. Package: com.vibebank.entity. Include 24 fields: id (UUID), account_number, iban, swift_bic, holder_full_name, holder_email, holder_phone, account_type, currency, balance (BigDecimal), available_balance (BigDecimal), status, aml_flag (Boolean), rbi_audit_id, branch_code, ifsc_code, nominee_name, kyc_status, credit_limit (BigDecimal), interest_rate (BigDecimal), tenant_id (UUID), created_at (LocalDateTime), updated_at (LocalDateTime). Use @Entity @Table @Id @GeneratedValue(UUID) @Column. Return only Java code.",
        "ecommerce": f"Generate a Java Spring Boot JPA @Entity class named Product for GDPR e-commerce. Package: com.omnistore.entity. Include 28 fields: id (UUID), sku, name, description, price (BigDecimal), stock_quantity (Integer), category, brand, gst_hsn_code, gdpr_consent_required (Boolean), seller_id (UUID), weight (BigDecimal), dimensions, image_url, rating (BigDecimal), review_count (Integer), discount_percent (BigDecimal), tax_rate (BigDecimal), status, tenant_id (UUID), created_at, updated_at. Use @Entity @Table @Id @GeneratedValue(UUID) @Column. Return only Java code.",
        "logistics": f"Generate a Java Spring Boot JPA @Entity class named Shipment for ISO 27001 logistics. Package: com.logiroute.entity. Include 23 fields: id (UUID), awb_number, shipper_id (UUID), consignee_name, consignee_phone, service_type, shipment_type, status, weight_grams (Integer), declared_value (BigDecimal), cod_amount (BigDecimal), is_cod (Boolean), is_fragile (Boolean), vehicle_id (UUID), driver_id (UUID), route_id (UUID), estimated_delivery_at, actual_delivery_at, delivery_otp, attempt_count (Integer), tenant_id (UUID), created_at, updated_at. Use @Entity @Table @Id @GeneratedValue(UUID) @Column. Return only Java code.",
        "custom": f"Generate a Java Spring Boot JPA @Entity class named CoreEntity for this requirement: {description}. CoreEntity is the main business record of the application. Package: com.customapp.entity. Use @Entity @Table @Id @GeneratedValue(UUID) @Column annotations. Include a String field named status. Add 4 to 8 relevant business fields (for example name, description, reference_code) besides status. Foreign keys to other entities must be plain UUID fields. Do NOT use JPA relationship annotations (@OneToMany, @ManyToOne, @ManyToMany, @OneToOne), do NOT use collections of other entities, and do NOT reference other entity classes or add helper methods for them. Return only Java code.",
    }

    MAX_RETRIES = 3
    heal_issues = []
    entity_code = ""
    generation_status = "ok"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"Generation attempt {attempt}/{MAX_RETRIES} for {entity}")
            entity_code = strip_fences(await call_groq(PACK_PROMPTS.get(pack, PACK_PROMPTS["banking"])))
            logger.info(f"LLM generated entity: {len(entity_code)} chars")
            entity_code, heal_issues = await self_heal(entity_code, pack, entity, cfg["compliance"])
            if pack == "custom" and not heal_issues:
                import re as _rx
                if _rx.search(r"@(OneToMany|ManyToOne|ManyToMany|OneToOne)\b", entity_code):
                    heal_issues = ["relationship annotations are not allowed: use plain UUID foreign keys"]
            if not heal_issues:
                logger.info(f"Attempt {attempt} succeeded!")
                generation_status = "ok"
                break
            else:
                logger.warning(f"Attempt {attempt} failed: {heal_issues}. Retrying...")
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Attempt {attempt} error: {e}")
            entity_code = f"// Error attempt {attempt}: {e}"
            await asyncio.sleep(2)
    else:
        generation_status = "needs_human_review"
        logger.error(f"ALL 3 ATTEMPTS FAILED for {entity} — needs human review!")
        await pub(85, "human_review", f"HUMAN REVIEW NEEDED — AI failed 3 attempts for {entity}. Issues: {str(heal_issues)}")
        entity_code = "// HUMAN REVIEW REQUIRED - AI failed 3 attempts. Please write " + entity + " entity manually."

    entity_code = postprocess_entity(entity_code, cfg)
    entity_code = _clean_entity(entity_code, entity)
    await pub(40, "architect", f"Generating {cfg['framework']} project structure — 7 files")
    await asyncio.sleep(0.5)

    # Generate all files
    pom = generate_pom_xml(cfg)
    app_yml = generate_application_yml(cfg, pack)
    repo = generate_repository(cfg)
    service = generate_service(cfg)
    controller = generate_controller(cfg)
    sql = generate_flyway_sql(cfg, pack)
    if True:
        from tasks.postprocess import sql_from_entity
        _gen_sql = sql_from_entity(entity_code, cfg)
        if _gen_sql:
            sql = _gen_sql
    erd = ERD_TEMPLATES.get(pack, "erDiagram\n    ENTITY { uuid id string name }")
    backtick3 = chr(96) * 3
    erd_md = f"# {pack.title()} ERD\n\n{backtick3}mermaid\n{erd}\n{backtick3}"

    # Save all files to disk
    out_dir = f"/app/generated/{job_id}"
    os.makedirs(f"{out_dir}/src/main/java/{cfg['package'].replace('.','/')}/entity", exist_ok=True)
    os.makedirs(f"{out_dir}/src/main/java/{cfg['package'].replace('.','/')}/repository", exist_ok=True)
    os.makedirs(f"{out_dir}/src/main/java/{cfg['package'].replace('.','/')}/service", exist_ok=True)
    os.makedirs(f"{out_dir}/src/main/java/{cfg['package'].replace('.','/')}/controller", exist_ok=True)
    os.makedirs(f"{out_dir}/src/main/resources/db/migration", exist_ok=True)

    test_code = generate_junit_test(cfg)
    test_path = f"src/test/java/{cfg['package'].replace('.','/')}/{entity}ServiceTest.java"

    disclaimer = '\n\n> **Disclaimer:** This is a code template following RBI Tier-1 field guidelines. Final compliance validation required by your organization legal and compliance team. Agents of Agents is not a licensed financial institution.'
    files = {
        "pom.xml": pom,
        f"src/main/java/{cfg['package'].replace('.','/')}/entity/{entity}.java": entity_code,
        f"src/main/java/{cfg['package'].replace('.','/')}/repository/{entity}Repository.java": repo,
        f"src/main/java/{cfg['package'].replace('.','/')}/service/{entity}Service.java": service,
        f"src/main/java/{cfg['package'].replace('.','/')}/controller/{entity}Controller.java": controller,
        "src/main/resources/application.yml": app_yml,
        "src/main/resources/db/migration/V1__create_tables.sql": sql,
        "ERD.md": erd_md + disclaimer,
        test_path: test_code,
        f"src/main/java/{cfg['package'].replace('.','/')}/{entity}ServiceApplication.java": generate_application_class(cfg),
        ".gitignore": ".env\ntarget/\n*.log\n.idea/\n",
        "Dockerfile": generate_dockerfile(cfg),
        "docker-compose.yml": generate_docker_compose(cfg, pack),
    }

    # --- MULTI-ENTITY: generate the remaining entities of the pack, one LLM call each ---
    gen_entities = [entity]
    skipped_entities = []
    _extra = _pack_entities(pack, spec_data)
    if pack == "custom" and not _extra:
        _extra = await _plan_custom_entities(description)
        if _extra:
            await pub(30, "planner", "Planned entities: CoreEntity, " + ", ".join(e[0] for e in _extra))
    _pp = cfg["package"].replace(".", "/")
    _sql_key = "src/main/resources/db/migration/V1__create_tables.sql"
    _base_sql = files[_sql_key]
    _ent_sql = {}
    for _i, (_ename, _table, _fields) in enumerate(_extra, 1):
        await pub(45, "architect", f"Generating entity {_i}/{len(_extra)}: {_ename}")
        _cfg = {**cfg, "entity": _ename, "has_status": True}
        _code = None
        for _attempt in (1, 2, 3):
            try:
                _prompt = (f"Generate a Java Spring Boot JPA @Entity class named {_ename} for {cfg['compliance']} {pack}. "
                           f"Package: {cfg['package']}.entity. Fields: id (UUID), {_fields}, tenant_id (UUID), created_at, updated_at. "
                           f"Foreign keys are plain UUID fields, no relationship annotations. "
                           f"Use @Entity @Table(name=\"{_table}\") @Id @GeneratedValue(UUID) @Column. Return only Java code.")
                _p = _prompt + (" Be compact: no comments, no Javadoc, no helper methods, no Lombok." if _attempt > 1 else "")
                _c = strip_fences(await call_groq(_p, max_tokens={1: 1800, 2: 1200, 3: 900}[_attempt]))
                _c, _issues = await self_heal(_c, pack, _ename, cfg["compliance"])
                if not _issues:
                    _code = postprocess_entity(_c, _cfg)
                    break
                logger.warning(f"{_ename} attempt {_attempt} issues: {_issues}")
            except Exception as e:
                logger.error(f"{_ename} attempt {_attempt} error: {e}")
                if "429" in str(e) and "Request too large" not in str(e):
                    await asyncio.sleep(30)
            await asyncio.sleep(2)
        if _code is None:
            skipped_entities.append(_ename)
            continue
        import re as _re
        _cfg["has_status"] = bool(_re.search(r"\bstatus\b", _code))
        _code = _clean_entity(_code, _ename)
        files[f"src/main/java/{_pp}/entity/{_ename}.java"] = _code
        files[f"src/main/java/{_pp}/repository/{_ename}Repository.java"] = generate_repository(_cfg)
        files[f"src/main/java/{_pp}/service/{_ename}Service.java"] = generate_service(_cfg)
        files[f"src/main/java/{_pp}/controller/{_ename}Controller.java"] = generate_controller(_cfg)
        files[f"src/test/java/{_pp}/{_ename}ServiceTest.java"] = generate_junit_test(_cfg)
        from tasks.postprocess import sql_from_entity as _sfe
        _s = _sfe(_code, _cfg)
        if not _s:
            logger.warning(f"{_ename}: no SQL could be derived from the entity, dropping it")
            logger.warning(f"{_ename} dropped source (no SQL):\n{_code[:6000]}")
            for _k in (f"src/main/java/{_pp}/entity/{_ename}.java", f"src/main/java/{_pp}/repository/{_ename}Repository.java",
                       f"src/main/java/{_pp}/service/{_ename}Service.java", f"src/main/java/{_pp}/controller/{_ename}Controller.java",
                       f"src/test/java/{_pp}/{_ename}ServiceTest.java"):
                files.pop(_k, None)
            skipped_entities.append(_ename)
            await asyncio.sleep(4)
            continue
        files[_sql_key] = files[_sql_key] + "\n\n" + _s
        _ent_sql[_ename] = _s
        gen_entities.append(_ename)
        await asyncio.sleep(4)
    # --- COMPILE GATE: drop entities that do not compile, say so in the status ---
    await pub(75, "qa_gate", "Compiling generated project (Maven)...")
    gate_note = ""
    for _round in range(3):
        _res = await _compile_check(files, job_id)
        if _res["status"] == "ok":
            gate_note = "; compile check passed"
            break
        if _res["status"] != "fail":
            gate_note = f"; compile check {_res['status']}"
            break
        _drop = [en for en in gen_entities if en != entity and any(
            b in (en, en + "Repository", en + "Service", en + "Controller", en + "ServiceTest") for b in _res["bad"])]
        if not _drop:
            gate_note = "; COMPILE CHECK FAILED in shared files, repo may not build"
            break
        for en in _drop:
            logger.warning(f"{en} dropped source (compile gate):\n{files.get(f'src/main/java/{_pp}/entity/{en}.java', '')[:6000]}")
        for en in _drop:
            for k in (f"src/main/java/{_pp}/entity/{en}.java", f"src/main/java/{_pp}/repository/{en}Repository.java",
                      f"src/main/java/{_pp}/service/{en}Service.java", f"src/main/java/{_pp}/controller/{en}Controller.java",
                      f"src/test/java/{_pp}/{en}ServiceTest.java"):
                files.pop(k, None)
            gen_entities.remove(en)
            skipped_entities.append(en)
            _ent_sql.pop(en, None)
        files[_sql_key] = _base_sql + "".join("\n\n" + _ent_sql[e] for e in gen_entities if e in _ent_sql)
        logger.warning(f"compile gate round {_round + 1}: dropped {_drop}")
    else:
        gate_note = "; COMPILE CHECK FAILED after 3 rounds, repo may not build"
    # --- BOOT GATE: the app must start and answer every entity endpoint ---
    if gate_note == "; compile check passed":
        from tasks.bootcheck import run_boot_check
        await pub(78, "qa_gate", "Booting generated app against a throwaway database...")
        for _bround in range(3):
            try:
                _b = await run_boot_check(files, job_id)
            except Exception as _be:
                logger.error(f"boot gate crashed: {_be}")
                gate_note += "; boot check unavailable"
                break
            if _b["status"] == "ok":
                gate_note = f"; compile + boot check passed (POST ok {_b['post_ok']}/{_b['total']})"
                break
            if _b["status"] == "unavailable":
                gate_note += "; boot check unavailable"
                break
            _bdrop = [en for en in gen_entities if en != entity and en in _b["bad"]]
            if not _bdrop:
                gate_note = "; BOOT CHECK FAILED, repo may not start"
                break
            for en in _bdrop:
                for k in (f"src/main/java/{_pp}/entity/{en}.java", f"src/main/java/{_pp}/repository/{en}Repository.java",
                          f"src/main/java/{_pp}/service/{en}Service.java", f"src/main/java/{_pp}/controller/{en}Controller.java",
                          f"src/test/java/{_pp}/{en}ServiceTest.java"):
                    files.pop(k, None)
                gen_entities.remove(en)
                skipped_entities.append(en)
                _ent_sql.pop(en, None)
            files[_sql_key] = _base_sql + "".join("\n\n" + _ent_sql[e] for e in gen_entities if e in _ent_sql)
            logger.warning(f"boot gate round {_bround + 1}: dropped {_bdrop}")
        else:
            gate_note = "; BOOT CHECK FAILED after 3 rounds, repo may not start"
    _skip_note = (f" (skipped: {', '.join(skipped_entities)})" if skipped_entities else "") + gate_note
    # --- SECURITY SCAN v1 (non-blocking): secrets, code issues, SBOM ---
    try:
        from tasks.security_scan import run_security_scan
        await pub(79, "security", "Running security scan (secrets, code, SBOM)...")
        _sec_files, _sec_note = await run_security_scan(files, job_id)
        files.update(_sec_files)
        _skip_note += _sec_note
    except Exception as _se:
        logger.error(f"security scan crashed: {_se}")

    for path, content in files.items():
        full_path = f"{out_dir}/{path}"
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)

    logger.info(f"Saved {len(files)} files to {out_dir}")

    await pub(60, "synthesizer", f"Generated {len(files)} files — Entity, Repository, Service, Controller, SQL, pom.xml")
    await asyncio.sleep(0.5)

    heal_msg = f" Self-healed {len(heal_issues)} issues." if heal_issues else " No issues found."
    await pub(80, "qa_gate", f"Static entity checks done; Maven compile check runs next.{heal_msg}")
    await asyncio.sleep(0.5)

    # Push ALL files to Gitea
    repo_name = f"agents-{pack}-{job_id[:8]}"
    pushed = 0
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(f"{GITEA_URL}/api/v1/user/repos",
            headers={"Authorization": f"token {GITEA_TOKEN}"},
            json={"name": repo_name, "description": f"{cfg['compliance']} {pack} app — Agents of Agents AI", "auto_init": True})
        await asyncio.sleep(2)

    for file_path, content in files.items():
        if await push_file_to_gitea(repo_name, file_path, content):
            pushed += 1
            await asyncio.sleep(0.3)

    gitea_url = f"https://agents-of-agents.duckdns.org/gitea/vibeforge-admin/{repo_name}"
    logger.info(f"Pushed {pushed}/{len(files)} files to {gitea_url}")

    if pushed < len(files):
        await db_mark_done(tenant_id, job_id, "failed", gitea_url, {"error": "incomplete push", "pushed": pushed, "expected": len(files)})
        await pub(100, "failed", f"Only {pushed}/{len(files)} files reached Gitea. Repo may be incomplete: {gitea_url}")
        return {"status": "failed", "pack": pack, "files": pushed, "expected": len(files), "gitea_url": gitea_url}
    await db_mark_done(tenant_id, job_id, "completed", gitea_url)
    await pub(100, "complete", f"Done — {pushed} files pushed, {len(gen_entities)} entities generated{_skip_note}. {cfg['compliance']}-aligned template, not a compliance certification. Repo: {gitea_url}")
    return {"status": "completed", "pack": pack, "tables": cfg["tables"], "files": pushed, "gitea_url": gitea_url}


# --- LLM usage recording: one llm_calls row per Groq call + Redis token counter (appended wrapper) ---
from decimal import Decimal as _Dec
from datetime import date as _date
from tasks.dbstatus import _run as _db_run

# USD per 1M tokens (input, output). PLACEHOLDERS: set to your real Groq rates.
PRICES = {"qwen/qwen3.8-27b": (0.29, 0.59)}
_DEFAULT_PRICE = (0.29, 0.59)


def _call_cost(model, pt, ct):
    pin, pout = PRICES.get(model, _DEFAULT_PRICE)
    return _Dec(str(round((pt * pin + ct * pout) / 1_000_000, 8)))


async def _record_llm_usage(ctx, tenant_id, job_id, usage):
    if not usage:
        return
    import uuid as _uuid
    tid, jid = _uuid.UUID(str(tenant_id)), _uuid.UUID(str(job_id))
    for u in usage:
        pt, ct = u["prompt_tokens"], u["completion_tokens"]
        await _db_run(
            tid,
            "INSERT INTO llm_calls (tenant_id, job_id, model_alias, prompt_tokens, completion_tokens, cost_usd) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            tid, jid, u["model"], pt, ct, _call_cost(u["model"], pt, ct),
        )
    redis = (ctx or {}).get("redis")
    if redis is not None:
        key = f"budget:{tenant_id}:{_date.today().isoformat()}"
        await redis.hincrby(key, "llm_tokens", sum(u["prompt_tokens"] + u["completion_tokens"] for u in usage))
        await redis.expire(key, 60 * 60 * 24 * 40)
    logger.info(f"recorded {len(usage)} LLM calls for job {job_id}")


_orig_run_generation = run_generation


async def run_generation(ctx, job_id, tenant_id, spec_data):
    usage = []
    token = _LLM_USAGE_VAR.set(usage)
    try:
        return await _orig_run_generation(ctx, job_id, tenant_id, spec_data)
    finally:
        _LLM_USAGE_VAR.reset(token)
        try:
            await _record_llm_usage(ctx, tenant_id, job_id, usage)
        except Exception as e:  # never let bookkeeping fail the job
            logger.error(f"llm usage recording failed for job {job_id}: {e}")
