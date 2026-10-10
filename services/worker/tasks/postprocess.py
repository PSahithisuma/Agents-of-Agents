import re

def strip_fences(text):
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"```[A-Za-z]*\s*\n(.*?)```", t, flags=re.S)
    if m:
        t = m.group(1).strip()
    elif t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.replace("```", "").strip()
    return t + "\n"

def table_name(entity):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", entity).lower() + "s"

def _base_postprocess(code, cfg):
    table = table_name(cfg["entity"])
    code = re.sub(r'(@Table\s*\(\s*name\s*=\s*")[^"]*(")',
                  lambda m: m.group(1) + table + m.group(2), code, count=1)
    if "PrePersist" not in code:
        m = re.search(r"private\s+(?:java\.time\.)?(LocalDateTime|Instant)\s+createdAt\s*;", code)
        if m:
            has_upd = re.search(r"private\s+(?:java\.time\.)?(?:LocalDateTime|Instant)\s+updatedAt\s*;", code) is not None
            _T = m.group(1)
            hook = "\n\n    @jakarta.persistence.PrePersist\n    protected void onCreate() {\n        java.time." + _T + " now = java.time." + _T + ".now();\n        if (this.createdAt == null) this.createdAt = now;\n"
            if has_upd:
                hook += "        if (this.updatedAt == null) this.updatedAt = now;\n"
            hook += "    }\n"
            if has_upd and "PreUpdate" not in code:
                hook += "\n    @jakarta.persistence.PreUpdate\n    protected void onUpdate() {\n        this.updatedAt = java.time." + _T + ".now();\n    }\n"
            code = code[:m.end()] + hook + code[m.end():]
    return code

def generate_application_class(cfg):
    cls = cfg["entity"] + "ServiceApplication"
    return (
        "package " + cfg["package"] + ";\n\n"
        "import org.springframework.boot.SpringApplication;\n"
        "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
        "@SpringBootApplication\n"
        "public class " + cls + " {\n"
        "    public static void main(String[] args) {\n"
        "        SpringApplication.run(" + cls + ".class, args);\n"
        "    }\n"
        "}\n"
    )


def _camel(name):
    parts = name.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])

def camelize_entity(code):
    import re as _re
    fields = _re.findall(r"private\s+[\w<>.,\s]+?\s+([a-z][A-Za-z0-9]*_[A-Za-z0-9_]*)\s*;", code)
    for f in dict.fromkeys(fields):
        c = _camel(f)
        F, C = f[:1].upper() + f[1:], c[:1].upper() + c[1:]
        code = _re.sub(r"\b" + _re.escape(f) + r"\b", c, code)
        code = _re.sub(r"\b(get|set|is)" + _re.escape(F) + r"\b", lambda m: m.group(1) + C, code)
    return code


def snakeize_column_names(code):
    import re as _re
    def fix(m):
        name = _re.sub(r"(?<!^)(?=[A-Z])", "_", m.group(2)).lower()
        return m.group(1) + name + m.group(3)
    return _re.sub(r'(@Column\s*\(\s*name\s*=\s*")([^"]+)(")', fix, code)


def fix_jakarta(code):
    return (code.replace("javax.persistence", "jakarta.persistence")
                .replace("javax.validation", "jakarta.validation"))


_SQL_TYPES = {
    "UUID": "UUID", "String": "VARCHAR(255)", "Integer": "INTEGER", "int": "INTEGER",
    "Long": "BIGINT", "long": "BIGINT", "BigDecimal": "NUMERIC(19,4)", "Boolean": "BOOLEAN",
    "boolean": "BOOLEAN", "Double": "DOUBLE PRECISION", "double": "DOUBLE PRECISION",
    "LocalDateTime": "TIMESTAMP", "LocalDate": "DATE", "Instant": "TIMESTAMP",
}

def _strip_nested_classes(code):
    """Drop nested member classes (a model-added Builder, say) so only the entity's own fields stay."""
    import re as _re
    pat = _re.compile(r"\n[ \t]+(?:(?:public|private|protected|static|final)\s+)*class\s+\w+[^{;]*\{")
    while True:
        m = pat.search(code)
        if not m:
            return code
        i, depth = m.end(), 1
        while i < len(code) and depth:
            depth += (code[i] == "{") - (code[i] == "}")
            i += 1
        code = code[:m.start()] + code[i:]


def sql_from_entity(code, cfg):
    import re as _re
    table = table_name(cfg["entity"])
    cols = []
    pat = r'(?:@Column\s*\(([^)]*)\)\s*)?private\s+([A-Za-z][A-Za-z0-9_.]*)\s+([a-z][A-Za-z0-9]*)\s*(?:=[^;]*)?;'
    _body = _strip_nested_classes(code)
    for m in _re.finditer(pat, _body):
        attrs, jtype, fname = m.group(1) or "", m.group(2), m.group(3)
        jtype = jtype.split(".")[-1]
        nm = _re.search(r'name\s*=\s*"([^"]+)"', attrs)
        col = nm.group(1) if nm else _re.sub(r"(?<!^)(?=[A-Z])", "_", fname).lower()
        sqltype = _SQL_TYPES.get(jtype)
        if sqltype is None and "@Enumerated" in code and jtype[:1].isupper() and (
                _re.search(r"enum\s+" + jtype + r"\b", code) or _re.search(r"@Enumerated[^;]*?\b" + jtype + r"\s+" + fname + r"\s*;", code)):
            sqltype = "VARCHAR(30)"
        if sqltype is None:
            continue
        ln = _re.search(r'length\s*=\s*(\d+)', attrs)
        if sqltype == "VARCHAR(255)" and ln:
            sqltype = "VARCHAR(%s)" % ln.group(1)
        line = "    %s %s" % (col, sqltype)
        if col == "id":
            line += " PRIMARY KEY DEFAULT gen_random_uuid()"
        else:
            if "nullable = false" in attrs and col not in ("tenant_id", "created_at", "updated_at"):
                line += " NOT NULL"
                if sqltype in ("INTEGER", "BIGINT", "NUMERIC(19,4)", "DOUBLE PRECISION"):
                    line += " DEFAULT 0"
            if "unique = true" in attrs:
                line += " UNIQUE"
        cols.append(line)
    _nfields = len(_re.findall(r"private\s+[A-Za-z][A-Za-z0-9_.]*\s+[a-z][A-Za-z0-9]*\s*(?:=[^;]*)?;", _body))
    if len(cols) != _nfields:
        return None
    if not any(c.strip().startswith("id ") for c in cols) or len(cols) < 3:
        return None
    return "CREATE TABLE IF NOT EXISTS %s (\n%s\n);\n" % (table, ",\n".join(cols))

def init_required_defaults(code):
    import re as _re
    pat = r'(@Column\s*\([^)]*nullable\s*=\s*false[^)]*\)[^;{}]*?private\s+)(Boolean|Integer|Long|BigDecimal)(\s+[a-z][A-Za-z0-9]*)\s*;'
    init = {"Boolean": "Boolean.FALSE", "Integer": "0", "Long": "0L", "BigDecimal": "java.math.BigDecimal.ZERO"}
    return _re.sub(pat, lambda m: m.group(1) + m.group(2) + m.group(3) + " = " + init[m.group(2)] + ";", code)


def add_not_null(code):
    import re as _re
    pat = r'(@Column\s*\([^)]*nullable\s*=\s*false[^)]*\)\s*)(private\s+(?:UUID|String)\s+(?!id\b|tenantId\b)[a-z][A-Za-z0-9]*\s*;)'
    return _re.sub(pat, lambda m: m.group(1) + '@jakarta.validation.constraints.NotNull ' + m.group(2), code)


def fix_class_name(code, cfg):
    import re as _re
    want = cfg["entity"]
    m = _re.search(r"public\s+class\s+([A-Za-z0-9_]+)", code)
    if not m or m.group(1) == want:
        return code
    have = m.group(1)
    return _re.sub(r"\b" + _re.escape(have) + r"\b", want, code)




def repair_truncated(code):
    """If the LLM output was cut off mid-class, drop the partial member and close the class."""
    if code.count("{") == code.count("}"):
        return code
    lines = code.rstrip().split("\n")
    while lines and lines[-1] != "    }":
        lines.pop()
    return ("\n".join(lines) + "\n}\n") if lines else code

def add_missing_accessors(code):
    import re as _re
    body = code.rstrip()
    if not body.endswith("}"):
        return code
    add = ""
    for line in body.split("\n"):
        if not line.startswith("    ") or line.startswith("     ") and "private" not in line:
            continue
        if "static" in line or "final" in line:
            continue
        m = _re.search(r"private\s+([\w.<>\[\], ]+?)\s+([a-z]\w*)\s*(?:=[^;]*)?;", line)
        if not m:
            continue
        typ, name = m.group(1), m.group(2)
        N = name[0].upper() + name[1:]
        if ("get%s(" % N) not in body and ("is%s(" % N) not in body:
            add += "\n    public %s get%s() {\n        return %s;\n    }\n" % (typ, N, name)
        if ("set%s(" % N) not in body:
            add += "\n    public void set%s(%s %s) {\n        this.%s = %s;\n    }\n" % (N, typ, name, name, name)
    return (body[:-1].rstrip() + "\n" + add + "}\n") if add else code

def postprocess_entity(code, cfg):
    code = repair_truncated(code)
    code = fix_class_name(code, cfg)
    code = camelize_entity(code)
    code = _base_postprocess(code, cfg)
    code = snakeize_column_names(code)
    code = fix_jakarta(code)
    code = init_required_defaults(code)
    code = add_not_null(code)
    # tenant default in the same hook the base step creates
    if "tenantId" in code and "DEFAULT_TENANT" not in code:
        code = code.replace(
            "if (this.createdAt == null) this.createdAt = now;",
            "if (this.createdAt == null) this.createdAt = now;\n        if (this.tenantId == null) this.tenantId = java.util.UUID.fromString(\"00000000-0000-0000-0000-000000000001\"); // DEFAULT_TENANT placeholder",
            1)
    if "tenantId" in code and "DEFAULT_TENANT" not in code and "PrePersist" in code:
        _pm = re.search(r"@jakarta\.persistence\.PrePersist\s+(?:public |protected |private )?void\s+\w+\s*\(\s*\)\s*\{", code) or re.search(r"@PrePersist\s+(?:public |protected |private )?void\s+\w+\s*\(\s*\)\s*\{", code)
        if _pm:
            code = code[:_pm.end()] + "\n        if (this.tenantId == null) this.tenantId = java.util.UUID.fromString(\"00000000-0000-0000-0000-000000000001\"); // DEFAULT_TENANT" + code[_pm.end():]
    if "tenantId" in code and "DEFAULT_TENANT" not in code and "PrePersist" not in code:
        _m = re.search(r"private\s+(?:java\.util\.)?UUID\s+tenantId\s*;", code)
        if _m:
            code = code[:_m.end()] + "\n\n    @jakarta.persistence.PrePersist\n    protected void onCreateTenant() {\n        if (this.tenantId == null) this.tenantId = java.util.UUID.fromString(\"00000000-0000-0000-0000-000000000001\"); // DEFAULT_TENANT\n    }\n" + code[_m.end():]
    return ensure_imports(add_missing_accessors(code))


# --- dedupe hook: remove members the model wrote twice (appended; original pipeline untouched) ---
from tasks.dedupe_members import dedupe_class_members as _dedupe_members
_prev_postprocess_entity = postprocess_entity


def postprocess_entity(code, cfg):
    return _dedupe_members(_prev_postprocess_entity(code, cfg))


_STD_IMPORTS = {
    "UUID": "java.util.UUID", "BigDecimal": "java.math.BigDecimal",
    "LocalDate": "java.time.LocalDate", "LocalDateTime": "java.time.LocalDateTime",
    "Instant": "java.time.Instant", "List": "java.util.List", "Set": "java.util.Set",
    "Map": "java.util.Map", "Optional": "java.util.Optional",
}


def ensure_imports(code):
    """Add standard imports the model used but forgot (compile gate: cannot find symbol)."""
    import re as _re
    body = _re.sub(r"^import [^;]+;[ \t]*$", "", code, flags=_re.M)
    missing = []
    for simple, fq in _STD_IMPORTS.items():
        if not _re.search(r"(?<![\w.])" + simple + r"\b", body):
            continue
        pkg = fq.rsplit(".", 1)[0]
        if _re.search(r"^import\s+(?:" + _re.escape(fq) + "|" + _re.escape(pkg) + r"\.\*)\s*;", code, flags=_re.M):
            continue
        missing.append(fq)
    if not missing:
        return code
    lines = "".join("import %s;\n" % fq for fq in missing)
    m = _re.search(r"^package\s+[^;]+;[ \t]*\n", code, flags=_re.M)
    if m:
        return code[:m.end()] + lines + code[m.end():]
    return lines + code


# --- tenantId safety net -------------------------------------------------------------------------
# Generated repositories declare findByIdAndTenantId(...), so EVERY entity must have a tenantId property;
# otherwise Spring Data fails at startup. The model sometimes omits it, so guarantee it here.
def _has_field(code, name):
    """True only if the class declares a field called `name`, not if the word merely appears in a method or comment."""
    return bool(re.search(r"\b(?:private|protected|public)\s+(?:final\s+)?[\w.<>\[\],? ]+?\s+" + name + r"\s*(?:;|=)", code))


def ensure_tenant_field(code):
    if _has_field(code, "tenantId"):
        return code
    m = re.search(r"public\s+class\s+\w+[^{]*\{", code)
    if not m:
        return code
    field = '\n\n    @Column(name = "tenant_id")\n    private UUID tenantId;\n'
    return code[:m.end()] + field + code[m.end():]


_postprocess_entity_before_tenant = postprocess_entity


def postprocess_entity(code, cfg):
    return _postprocess_entity_before_tenant(ensure_tenant_field(code), cfg)


# --- audit-field safety net ------------------------------------------------------------------------
# The generated repository declares  @Query("... e.createdAt >= :cutoff")  and Hibernate validates it at startup,
# so every entity must have createdAt (and updatedAt, which the persist hooks set). Guarantee both.
def ensure_audit_fields(code):
    add = ""
    if not _has_field(code, "createdAt"):
        add += '\n    @Column(name = "created_at")\n    private LocalDateTime createdAt;\n'
    if not _has_field(code, "updatedAt"):
        add += '\n    @Column(name = "updated_at")\n    private LocalDateTime updatedAt;\n'
    if not add:
        return code
    m = re.search(r"public\s+class\s+\w+[^{]*\{", code)
    if not m:
        return code
    return code[:m.end()] + add + code[m.end():]


_postprocess_entity_before_audit = postprocess_entity


def postprocess_entity(code, cfg):
    return _postprocess_entity_before_audit(ensure_audit_fields(code), cfg)


# --- java.util.Date -> LocalDateTime -------------------------------------------------------------------------
# The persist hooks and SQL builder assume LocalDateTime. If the model used java.util.Date (or Timestamp),
# normalise the whole entity (fields, getters, setters) so the code compiles and Hibernate maps it.
def normalize_dates(code):
    if not re.search(r"\b(java\.util\.)?Date\b|\bTimestamp\b|\b(java\.time\.)?Instant\b", code):
        return code
    code = re.sub(r"new\s+(java\.util\.)?Date\s*\(\s*\)", "LocalDateTime.now()", code)
    code = re.sub(r"^\s*@Temporal\s*\([^)]*\)[ \t]*\n", "", code, flags=re.M)
    code = re.sub(r"^import\s+java\.util\.Date;[ \t]*\n", "", code, flags=re.M)
    code = re.sub(r"^import\s+java\.sql\.Timestamp;[ \t]*\n", "", code, flags=re.M)
    code = re.sub(r"^import\s+java\.time\.Instant;[ \t]*\n", "", code, flags=re.M)
    parts = re.split(r'("(?:\\.|[^"\\])*")', code)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"\b(java\.util\.)?Date\b", "LocalDateTime", parts[i])
        parts[i] = re.sub(r"\b(java\.sql\.)?Timestamp\b", "LocalDateTime", parts[i])
        parts[i] = re.sub(r"\b(java\.time\.)?Instant\b", "LocalDateTime", parts[i])
    code = "".join(parts)
    if "import java.time.LocalDateTime;" not in code:
        m = re.search(r"^package\s+[^;]+;[ \t]*\n", code, flags=re.M)
        if m:
            code = code[:m.end()] + "import java.time.LocalDateTime;\n" + code[m.end():]
    return code


_postprocess_entity_before_dates = postprocess_entity


def postprocess_entity(code, cfg):
    return _postprocess_entity_before_dates(normalize_dates(code), cfg)



def fix_uuid_generator(code):
    """Models often use @GenericGenerator without importing it. Use the standard JPA 3.1 UUID strategy instead."""
    if "GenericGenerator" not in code:
        return code
    code = re.sub(r"^[ \t]*@GenericGenerator\s*\(.*?\)[ \t]*\n", "", code, flags=re.M | re.S)
    code = re.sub(r"^import\s+org\.hibernate\.annotations\.GenericGenerator;[ \t]*\n", "", code, flags=re.M)
    code = re.sub(r"@GeneratedValue\s*\([^)]*generator[^)]*\)", "@GeneratedValue(strategy = GenerationType.UUID)", code)
    code = re.sub(r"@GeneratedValue(?!\s*\()", "@GeneratedValue(strategy = GenerationType.UUID)", code)
    if "GenerationType" in code and "import jakarta.persistence.*;" not in code and "import jakarta.persistence.GenerationType;" not in code:
        m = re.search(r"^package\s+[^;]+;[ \t]*\n", code, flags=re.M)
        if m:
            code = code[:m.end()] + "import jakarta.persistence.GenerationType;\n" + code[m.end():]
    return code


_postprocess_entity_before_uuid = postprocess_entity


def postprocess_entity(code, cfg):
    return _postprocess_entity_before_uuid(fix_uuid_generator(code), cfg)


def fix_string_id(code):
    """If the entity id is a String but the id is generated as UUID, make it a real UUID
    so it matches JpaRepository<Entity, UUID> and derived queries like findByIdAndTenantId(UUID, ..)."""
    import re as _re
    if not _re.search(r"private\s+String\s+id\s*;", code):
        return code
    code = _re.sub(r"private\s+String\s+id\s*;", "private UUID id;", code)
    code = _re.sub(r"public\s+String\s+getId\s*\(\s*\)", "public UUID getId()", code)
    code = _re.sub(r"public\s+void\s+setId\s*\(\s*String\s+id\s*\)", "public void setId(UUID id)", code)
    if "import java.util.UUID;" not in code:
        code = _re.sub(r"(package\s+[\w.]+;\s*)", r"\1\nimport java.util.UUID;\n", code, count=1)
    return code


_postprocess_entity_before_stringid = postprocess_entity


def postprocess_entity(code, cfg):
    code = _postprocess_entity_before_stringid(code, cfg)
    return fix_string_id(code)


import re as _re_gv


def fix_generated_value(code):
    """Model sometimes writes @GeneratedValue(...uuid()...), which does not exist; use GenerationType.UUID."""
    def _sub(m):
        if "uuid" in m.group(0).lower():
            return "@GeneratedValue(strategy = GenerationType.UUID)"
        return m.group(0)
    out = _re_gv.sub(r"@GeneratedValue\s*\((?:[^()]|\(\))*\)", _sub, code)
    if ("GenerationType.UUID" in out
            and "import jakarta.persistence.*;" not in out
            and "import jakarta.persistence.GenerationType;" not in out):
        out = _re_gv.sub(r"(package [^;]+;\s*)", r"\1\nimport jakarta.persistence.GenerationType;\n", out, count=1)
    return out


_postprocess_entity_before_genval = postprocess_entity


def postprocess_entity(*args, **kwargs):
    res = _postprocess_entity_before_genval(*args, **kwargs)
    return fix_generated_value(res) if isinstance(res, str) else res
