from pydantic import BaseModel
from typing import List


class DomainModel(BaseModel):
    entities: List[str]


class APIModel(BaseModel):
    endpoints: List[str]


class UIModel(BaseModel):
    screens: List[str]


class WorkflowModel(BaseModel):
    workflows: List[str]


class IntegrationModel(BaseModel):
    integrations: List[str]


class SecurityModel(BaseModel):
    auth_type: str


class ComplianceModel(BaseModel):
    rules: List[str]


class NFRModel(BaseModel):
    performance: str


class AcceptanceCriteria(BaseModel):
    tests: List[str]


class AppSpec(BaseModel):

    domain_model: DomainModel

    api_model: APIModel

    ui_model: UIModel

    workflow_model: WorkflowModel

    integration_model: IntegrationModel

    security_model: SecurityModel

    compliance_model: ComplianceModel

    nfr: NFRModel

    acceptance_criteria: AcceptanceCriteria

    canonical_hash: str