"""
Target schema for the 'employees' entity on the new platform.

Each field carries a list of aliases used by the mapping engine to score
how well a source column name matches a target field. Aliases are
intentionally allowed to overlap across fields in a couple of places
(e.g. "contact" appears under both email and manager_email) -- this is
what creates genuine, defensible mapping ambiguity for the agent to
escalate, rather than every column being a clean 1:1 guess.
"""

TARGET_SCHEMA = [
    {
        "name": "employee_id",
        "required": True,
        "type": "string",
        "aliases": ["empid", "employee id", "employee number", "emp id", "id", "emp no", "emp_no"],
    },
    {
        "name": "first_name",
        "required": True,
        "type": "string",
        "aliases": ["first", "firstname", "first name", "given name"],
    },
    {
        "name": "last_name",
        "required": True,
        "type": "string",
        "aliases": ["last", "lastname", "last name", "surname", "family name"],
    },
    {
        "name": "email",
        "required": True,
        "type": "email",
        "aliases": ["email address", "e-mail", "email", "work email", "contact", "contact email"],
    },
    {
        "name": "department",
        "required": True,
        "type": "string",
        "aliases": ["dept", "department", "division", "team"],
    },
    {
        "name": "job_title",
        "required": False,
        "type": "string",
        "aliases": ["title", "position", "job title", "role"],
    },
    {
        "name": "hire_date",
        "required": True,
        "type": "date",
        "aliases": ["doh", "hire date", "date of hire", "start date", "joined"],
    },
    {
        "name": "status",
        "required": False,
        "type": "string",
        "aliases": ["status", "active", "active?", "employment status"],
    },
    {
        "name": "manager_email",
        "required": False,
        "type": "email",
        "aliases": ["mgr email", "manager email", "manager", "reports to", "contact"],
    },
    {
        "name": "location",
        "required": False,
        "type": "string",
        "aliases": ["office", "location", "city", "site"],
    },
]

FIELD_NAMES = [f["name"] for f in TARGET_SCHEMA]
REQUIRED_FIELDS = [f["name"] for f in TARGET_SCHEMA if f["required"]]
FIELD_TYPES = {f["name"]: f["type"] for f in TARGET_SCHEMA}

# A single source column, once split or mapped, is not itself a target field.
# 'full_name' is a virtual/pseudo column the ingestion step creates when a
# source file has one combined name column instead of first/last.
VIRTUAL_FULL_NAME_ALIASES = ["name", "full name", "employee name"]
