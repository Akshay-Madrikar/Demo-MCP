"""
AMC (Annual Maintenance Contract) Tracker - single-file MCP server

Generic: works for any business that sells maintenance contracts (HVAC, IT,
medical equipment, CCTV, water purifiers, generators, software support...).

FEATURES
  1. Create an AMC (customer, mobile and/or email, start + end date, plan,
     covered items, amount)
  2. View / search existing AMCs
  3. Edit AMC details (including cancel / reinstate)
  4. Free services per contract year (default 4, configurable per AMC) with
     automatic free vs chargeable tracking

STORAGE
  No database. Data is kept in Python dicts and saved to a JSON file after
  EVERY change (atomic write), so nothing is lost on restart.
  Default file: amc_data.json next to this script (override: AMC_DATA_FILE).

HOW IT RUNS (AMC_TRANSPORT)
  stdio  - local use with Claude Desktop / MCP Inspector   (default locally)
  http   - public/remote server at /mcp, plus /health      (default when the
           PORT env var exists, as on Render)

ENVIRONMENT VARIABLES
  AMC_BUSINESS_NAME   business name                         (first run default)
  AMC_CURRENCY        e.g. INR, USD                         (first run default)
  AMC_FREE_SERVICES   default free services per year, 4     (first run default)
  AMC_LOAD_SAMPLE     1 = seed sample data on first run, 0 = start empty
  AMC_DATA_FILE       path of the JSON data file (use /var/data/... on Render)
  AMC_TRANSPORT       stdio | http
  PORT                HTTP port (Render sets it)
  Access control for http mode (choose one):
  AMC_API_TOKEN       secret (24+ chars); clients send
                      "Authorization: Bearer <token>"
  AMC_ALLOW_NO_AUTH   1 = NO sign-in, anyone with the URL can read and change
                      all data. Used only when no token is set.
  AMC_EXTRA_HOSTS     comma-separated extra hostnames (custom domain)

RUN
  Local:   python main.py                     (stdio)
  Server:  AMC_TRANSPORT=http AMC_ALLOW_NO_AUTH=1 python main.py
  Needs:   pip install mcp uvicorn
"""

import hmac
import json
import os
import re
import tempfile
import threading
from datetime import date, datetime
from typing import Optional

from mcp.server import MCPServer


# ==========================================================================
# CONFIG
# ==========================================================================
DATA_FILE = os.environ.get(
    "AMC_DATA_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "amc_data.json"),
)

CONFIG = {
    "business_name": os.environ.get("AMC_BUSINESS_NAME", "My Business"),
    "currency": os.environ.get("AMC_CURRENCY", "INR"),
    # Free services included per contract year. Each AMC can override this.
    "default_free_services_per_year": int(os.environ.get("AMC_FREE_SERVICES", "4")),
}
LOAD_SAMPLE_DATA = os.environ.get("AMC_LOAD_SAMPLE", "1") == "1"
PERSISTED_CONFIG_KEYS = ("business_name", "currency", "default_free_services_per_year")


# ==========================================================================
# DATA STORE (in memory, mirrored to the JSON file)
# ==========================================================================
AMCS: dict = {}       # amc_id -> contract record
SERVICES: dict = {}   # service_id -> service visit record
_next_amc_id = 1
_next_service_id = 1
_lock = threading.RLock()


def _load_sample_data() -> None:
    """Hard-coded sample data, used only on first run. Edit to suit."""
    AMCS.update({
        1: {"id": 1, "customer_name": "Sample Customer A", "mobile": "9820012345",
            "email": "customer.a@example.com", "start_date": "2026-03-15",
            "end_date": "2027-03-14", "covered_items": "Equipment set (3 units)",
            "plan_name": "Gold", "amount": 12000.0, "free_services_per_year": 4,
            "notes": "", "cancelled": False},
        2: {"id": 2, "customer_name": "Sample Customer B", "mobile": "9930067890",
            "email": None, "start_date": "2025-12-01", "end_date": "2026-11-30",
            "covered_items": "Main unit", "plan_name": "Standard", "amount": 6000.0,
            "free_services_per_year": 4, "notes": "Renewal due soon", "cancelled": False},
        3: {"id": 3, "customer_name": "Sample Company C", "mobile": None,
            "email": "accounts@company-c.example.com", "start_date": "2024-06-01",
            "end_date": "2025-05-31", "covered_items": "Office equipment",
            "plan_name": "Basic", "amount": 4000.0, "free_services_per_year": 2,
            "notes": "Expired - renewal pending", "cancelled": False},
        4: {"id": 4, "customer_name": "Sample Customer D", "mobile": "9769011223",
            "email": "customer.d@example.com", "start_date": "2027-01-01",
            "end_date": "2027-12-31", "covered_items": "Server + network",
            "plan_name": "Gold", "amount": 15000.0, "free_services_per_year": 4,
            "notes": "Starts next year", "cancelled": False},
    })
    SERVICES.update({
        1: {"id": 1, "amc_id": 1, "service_date": "2026-04-20", "contract_year": 1,
            "is_free": True, "performed_by": "Staff 1", "notes": "Routine service"},
        2: {"id": 2, "amc_id": 1, "service_date": "2026-07-18", "contract_year": 1,
            "is_free": True, "performed_by": "Staff 1", "notes": "Routine service"},
        3: {"id": 3, "amc_id": 2, "service_date": "2026-01-25", "contract_year": 1,
            "is_free": True, "performed_by": "Staff 2", "notes": ""},
        4: {"id": 4, "amc_id": 2, "service_date": "2026-04-30", "contract_year": 1,
            "is_free": True, "performed_by": "Staff 2", "notes": ""},
        5: {"id": 5, "amc_id": 2, "service_date": "2026-08-02", "contract_year": 1,
            "is_free": True, "performed_by": "Staff 2", "notes": ""},
        6: {"id": 6, "amc_id": 2, "service_date": "2026-09-10", "contract_year": 1,
            "is_free": True, "performed_by": "Staff 2", "notes": ""},
        7: {"id": 7, "amc_id": 2, "service_date": "2026-10-15", "contract_year": 1,
            "is_free": False, "performed_by": "Staff 2", "notes": "Extra visit - chargeable"},
    })


def save_data() -> None:
    """Write all data to DATA_FILE atomically. Called after every change."""
    with _lock:
        payload = {
            "config": {k: CONFIG[k] for k in PERSISTED_CONFIG_KEYS},
            "next_amc_id": _next_amc_id,
            "next_service_id": _next_service_id,
            "amcs": {str(k): v for k, v in AMCS.items()},
            "services": {str(k): v for k, v in SERVICES.items()},
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        directory = os.path.dirname(DATA_FILE) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".amc_tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, DATA_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise


def load_data() -> None:
    """Load from DATA_FILE, or create it on first run."""
    global _next_amc_id, _next_service_id
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            AMCS.update({int(k): v for k, v in raw["amcs"].items()})
            SERVICES.update({int(k): v for k, v in raw["services"].items()})
            for k in PERSISTED_CONFIG_KEYS:
                if k in raw.get("config", {}):
                    CONFIG[k] = raw["config"][k]
            _next_amc_id = raw.get("next_amc_id", 1)
            _next_service_id = raw.get("next_service_id", 1)
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            # Never overwrite a file we could not read: that would destroy data.
            raise RuntimeError(
                f"Could not read data file {DATA_FILE!r}: {exc}. "
                "Fix or move the file and restart."
            ) from exc
    elif LOAD_SAMPLE_DATA:
        _load_sample_data()

    _next_amc_id = max(_next_amc_id, max(AMCS, default=0) + 1)
    _next_service_id = max(_next_service_id, max(SERVICES, default=0) + 1)
    if not os.path.exists(DATA_FILE):
        save_data()


load_data()

mcp = MCPServer(f"{CONFIG['business_name']} - AMC Tracker")


# ==========================================================================
# Helpers
# ==========================================================================
def parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ValueError(f"{field} must be in YYYY-MM-DD format, got {value!r}")


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year + n, day=28)


def contract_year(start: date, on: date) -> int:
    """Contract year number (1-based) that `on` falls into."""
    n = on.year - start.year
    if add_years(start, n) > on:
        n -= 1
    return n + 1


def contract_year_period(start: date, year_no: int, end: date):
    ys = add_years(start, year_no - 1)
    ye = min(add_years(start, year_no) - date.resolution, end)
    return ys, ye


def validate_contact(mobile: Optional[str], email: Optional[str]):
    mobile = (mobile or "").strip() or None
    email = (email or "").strip().lower() or None
    if not mobile and not email:
        raise ValueError("Provide at least one of mobile number or email.")
    if mobile and not re.fullmatch(r"\+?\d[\d\s\-]{7,14}\d", mobile):
        raise ValueError(f"Invalid mobile number: {mobile!r}")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError(f"Invalid email: {email!r}")
    return mobile, email


def validate_free_services(n: Optional[int]) -> int:
    if n is None:
        return CONFIG["default_free_services_per_year"]
    if n < 0:
        raise ValueError("free_services_per_year cannot be negative.")
    return n


def get_amc_or_fail(amc_id: int) -> dict:
    amc = AMCS.get(amc_id)
    if not amc:
        raise ValueError(f"AMC #{amc_id} not found.")
    return amc


def status_of(amc: dict) -> str:
    if amc["cancelled"]:
        return "cancelled"
    today = date.today()
    if today < date.fromisoformat(amc["start_date"]):
        return "upcoming"
    if today > date.fromisoformat(amc["end_date"]):
        return "expired"
    return "active"


def services_of(amc_id: int) -> list:
    return sorted(
        (s for s in SERVICES.values() if s["amc_id"] == amc_id),
        key=lambda s: (s["service_date"], s["id"]),
    )


def free_usage(amc: dict) -> list:
    """Per-contract-year free service summary."""
    start = date.fromisoformat(amc["start_date"])
    end = date.fromisoformat(amc["end_date"])
    allowed = amc["free_services_per_year"]
    svcs = services_of(amc["id"])
    out = []
    for y in range(1, contract_year(start, end) + 1):
        ys, ye = contract_year_period(start, y, end)
        used = sum(1 for s in svcs if s["contract_year"] == y and s["is_free"])
        out.append({
            "contract_year": y,
            "period": f"{ys} to {ye}",
            "free_allowed": allowed,
            "free_used": used,
            "free_remaining": max(allowed - used, 0),
        })
    return out


def reassign_services(amc: dict) -> None:
    """Recompute contract_year / is_free for all services of an AMC
    (after date changes, quota changes or deletions)."""
    start = date.fromisoformat(amc["start_date"])
    allowed = amc["free_services_per_year"]
    counts: dict = {}
    for s in services_of(amc["id"]):
        y = contract_year(start, date.fromisoformat(s["service_date"]))
        counts[y] = counts.get(y, 0) + 1
        s["contract_year"] = y
        s["is_free"] = counts[y] <= allowed


def amc_view(amc: dict, detail: bool = False) -> dict:
    usage = free_usage(amc)
    d = {
        "id": amc["id"],
        "customer_name": amc["customer_name"],
        "mobile": amc["mobile"],
        "email": amc["email"],
        "start_date": amc["start_date"],
        "end_date": amc["end_date"],
        "status": status_of(amc),
        "plan_name": amc["plan_name"],
        "covered_items": amc["covered_items"],
        "amount": amc["amount"],
        "currency": CONFIG["currency"],
        "free_services_per_year": amc["free_services_per_year"],
        "notes": amc["notes"],
    }
    if detail:
        d["free_services"] = usage
        d["services"] = [
            {k: s[k] for k in ("id", "service_date", "contract_year", "is_free", "performed_by", "notes")}
            for s in services_of(amc["id"])
        ]
    else:
        d["free_services"] = {
            "used": sum(u["free_used"] for u in usage),
            "total": sum(u["free_allowed"] for u in usage),
        }
    return d


# ==========================================================================
# Tools: configuration
# ==========================================================================
@mcp.tool()
def get_business_config() -> dict:
    """Show the business settings this tracker is running with (business name,
    currency, default free services per contract year) and the data file path."""
    return {**CONFIG, "data_file": DATA_FILE}


@mcp.tool()
def update_business_config(
    business_name: Optional[str] = None,
    currency: Optional[str] = None,
    default_free_services_per_year: Optional[int] = None,
) -> dict:
    """Change business settings (saved to the data file). The default free
    service count applies only to AMCs created afterwards; existing AMCs keep
    their own value (change them with update_amc)."""
    with _lock:
        if business_name:
            CONFIG["business_name"] = business_name.strip()
        if currency:
            CONFIG["currency"] = currency.strip().upper()
        if default_free_services_per_year is not None:
            CONFIG["default_free_services_per_year"] = validate_free_services(
                default_free_services_per_year)
        save_data()
    return {**CONFIG, "data_file": DATA_FILE}


# ==========================================================================
# Tools: AMC management
# ==========================================================================
@mcp.tool()
def create_amc(
    customer_name: str,
    start_date: str,
    end_date: str,
    mobile: Optional[str] = None,
    email: Optional[str] = None,
    covered_items: Optional[str] = None,
    plan_name: Optional[str] = None,
    amount: Optional[float] = None,
    free_services_per_year: Optional[int] = None,
    notes: Optional[str] = None,
) -> dict:
    """Create a new AMC. Dates are YYYY-MM-DD. At least one of mobile/email is
    required. covered_items = what the contract covers (equipment, site,
    software...). free_services_per_year defaults to the business setting."""
    global _next_amc_id
    if not customer_name.strip():
        raise ValueError("customer_name is required.")
    mobile, email = validate_contact(mobile, email)
    s, e = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if e <= s:
        raise ValueError("end_date must be after start_date.")
    if amount is not None and amount < 0:
        raise ValueError("amount cannot be negative.")
    quota = validate_free_services(free_services_per_year)

    with _lock:
        amc_id = _next_amc_id
        _next_amc_id += 1
        AMCS[amc_id] = {
            "id": amc_id,
            "customer_name": customer_name.strip(),
            "mobile": mobile,
            "email": email,
            "start_date": s.isoformat(),
            "end_date": e.isoformat(),
            "covered_items": covered_items,
            "plan_name": plan_name,
            "amount": amount,
            "free_services_per_year": quota,
            "notes": notes,
            "cancelled": False,
        }
        save_data()
        return amc_view(AMCS[amc_id], detail=True)


@mcp.tool()
def list_amcs(
    status: Optional[str] = None,
    search: Optional[str] = None,
    expiring_within_days: Optional[int] = None,
) -> list:
    """View existing AMCs.
    status: active | expired | upcoming | cancelled (optional)
    search: match on customer name, mobile, email, plan or covered items
    expiring_within_days: only active AMCs ending within N days (optional)"""
    result = []
    for amc in sorted(AMCS.values(), key=lambda a: a["end_date"]):
        st = status_of(amc)
        if status and st != status.lower():
            continue
        if search:
            hay = " ".join(
                str(amc[k] or "")
                for k in ("customer_name", "mobile", "email", "plan_name", "covered_items")
            ).lower()
            if search.lower() not in hay:
                continue
        if expiring_within_days is not None:
            days_left = (date.fromisoformat(amc["end_date"]) - date.today()).days
            if st != "active" or days_left > expiring_within_days:
                continue
        result.append(amc_view(amc))
    return result


@mcp.tool()
def get_amc(amc_id: int) -> dict:
    """Full details of one AMC, including per-year free-service balance and
    the complete service history."""
    return amc_view(get_amc_or_fail(amc_id), detail=True)


@mcp.tool()
def update_amc(
    amc_id: int,
    customer_name: Optional[str] = None,
    mobile: Optional[str] = None,
    email: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    covered_items: Optional[str] = None,
    plan_name: Optional[str] = None,
    amount: Optional[float] = None,
    free_services_per_year: Optional[int] = None,
    notes: Optional[str] = None,
    cancelled: Optional[bool] = None,
) -> dict:
    """Edit AMC details. Only the fields you pass are changed. Use
    cancelled=true to cancel (or false to reinstate). Dates cannot be changed
    in a way that leaves recorded services outside the contract period."""
    with _lock:
        amc = get_amc_or_fail(amc_id)

        name = customer_name.strip() if customer_name is not None else amc["customer_name"]
        if not name:
            raise ValueError("customer_name cannot be empty.")

        new_mobile, new_email = validate_contact(
            mobile if mobile is not None else amc["mobile"],
            email if email is not None else amc["email"],
        )

        s = parse_date(start_date, "start_date") if start_date else date.fromisoformat(amc["start_date"])
        e = parse_date(end_date, "end_date") if end_date else date.fromisoformat(amc["end_date"])
        if e <= s:
            raise ValueError("end_date must be after start_date.")
        if amount is not None and amount < 0:
            raise ValueError("amount cannot be negative.")

        dates_changed = (s.isoformat(), e.isoformat()) != (amc["start_date"], amc["end_date"])
        if dates_changed:
            outside = [x for x in services_of(amc_id)
                       if not (s <= date.fromisoformat(x["service_date"]) <= e)]
            if outside:
                raise ValueError(
                    f"{len(outside)} recorded service(s) fall outside the new dates. "
                    "Adjust or delete those services first."
                )

        new_quota = (validate_free_services(free_services_per_year)
                     if free_services_per_year is not None else amc["free_services_per_year"])
        quota_changed = new_quota != amc["free_services_per_year"]

        amc.update(
            customer_name=name,
            mobile=new_mobile,
            email=new_email,
            start_date=s.isoformat(),
            end_date=e.isoformat(),
            covered_items=covered_items if covered_items is not None else amc["covered_items"],
            plan_name=plan_name if plan_name is not None else amc["plan_name"],
            amount=amount if amount is not None else amc["amount"],
            free_services_per_year=new_quota,
            notes=notes if notes is not None else amc["notes"],
            cancelled=cancelled if cancelled is not None else amc["cancelled"],
        )
        if dates_changed or quota_changed:
            reassign_services(amc)
        save_data()
        return amc_view(amc, detail=True)


# ==========================================================================
# Tools: free-service tracking
# ==========================================================================
@mcp.tool()
def record_service(
    amc_id: int,
    service_date: Optional[str] = None,
    performed_by: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Record a service visit for an AMC (defaults to today). It is counted
    against that contract year's free services; once they are used up the
    visit is marked as chargeable (is_free=false)."""
    global _next_service_id
    with _lock:
        amc = get_amc_or_fail(amc_id)
        if amc["cancelled"]:
            raise ValueError("This AMC is cancelled.")

        start = date.fromisoformat(amc["start_date"])
        end = date.fromisoformat(amc["end_date"])
        on = parse_date(service_date, "service_date") if service_date else date.today()
        if not (start <= on <= end):
            raise ValueError(f"Service date {on} is outside the AMC period {start} to {end}.")

        allowed = amc["free_services_per_year"]
        year_no = contract_year(start, on)
        used = sum(1 for s in services_of(amc_id)
                   if s["contract_year"] == year_no and s["is_free"])
        is_free = used < allowed

        sid = _next_service_id
        _next_service_id += 1
        SERVICES[sid] = {
            "id": sid,
            "amc_id": amc_id,
            "service_date": on.isoformat(),
            "contract_year": year_no,
            "is_free": is_free,
            "performed_by": performed_by,
            "notes": notes,
        }
        save_data()
        return {
            "service_id": sid,
            "amc_id": amc_id,
            "service_date": on.isoformat(),
            "contract_year": year_no,
            "is_free": is_free,
            "free_remaining_this_year": max(allowed - used - (1 if is_free else 0), 0),
            "message": (
                f"Free service {used + 1} of {allowed} recorded for contract year {year_no}."
                if is_free
                else f"All {allowed} free services for contract year {year_no} are used. "
                     "This visit is chargeable."
            ),
        }


@mcp.tool()
def list_services(amc_id: int) -> dict:
    """Service history and free-service balance for an AMC."""
    d = amc_view(get_amc_or_fail(amc_id), detail=True)
    return {
        "amc_id": amc_id,
        "customer_name": d["customer_name"],
        "free_services": d["free_services"],
        "services": d["services"],
    }


@mcp.tool()
def delete_service(service_id: int) -> dict:
    """Delete a wrongly entered service record. Free/chargeable flags are
    re-evaluated so the free quota is restored."""
    with _lock:
        svc = SERVICES.pop(service_id, None)
        if not svc:
            raise ValueError(f"Service #{service_id} not found.")
        reassign_services(AMCS[svc["amc_id"]])
        save_data()
        return {"deleted": service_id, "amc_id": svc["amc_id"]}


# ==========================================================================
# Resources
# ==========================================================================
@mcp.resource("amc://{amc_id}")
def amc_resource(amc_id: str) -> str:
    """Readable summary of one AMC and its services."""
    d = amc_view(get_amc_or_fail(int(amc_id)), detail=True)
    amount = f"{d['currency']} {d['amount']:,.2f}" if d["amount"] is not None else "-"
    lines = [
        f"{CONFIG['business_name']}",
        f"AMC #{d['id']} - {d['customer_name']} [{d['status']}]",
        f"Contact : {d['mobile'] or '-'} / {d['email'] or '-'}",
        f"Period  : {d['start_date']} to {d['end_date']}",
        f"Plan    : {d['plan_name'] or '-'}   Amount: {amount}",
        f"Covers  : {d['covered_items'] or '-'}",
        "",
        "Free services:",
    ]
    for u in d["free_services"]:
        lines.append(f"  Year {u['contract_year']} ({u['period']}): "
                     f"{u['free_used']}/{u['free_allowed']} used")
    lines += ["", "Service history:"]
    lines += [f"  {s['service_date']}  {'FREE' if s['is_free'] else 'PAID'}  "
              f"{s['performed_by'] or ''}  {s['notes'] or ''}"
              for s in d["services"]] or ["  (none yet)"]
    return "\n".join(lines)


# ==========================================================================
# HTTP serving (public / Render) with optional bearer-token protection
# ==========================================================================
async def _send_json(send, status: int, body: dict, extra_headers=None) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(payload)).encode())]
    headers += extra_headers or []
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class TokenAuthMiddleware:
    """Pure ASGI middleware (safe for streaming responses).
    /health is always open. If a token is configured everything else needs
    it; if token is None, all requests are allowed."""

    def __init__(self, app, token):
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan events etc. pass straight through
            await self.app(scope, receive, send)
            return

        if scope.get("path") == "/health":
            await _send_json(send, 200, {"status": "ok"})
            return

        supplied = dict(scope["headers"]).get(b"authorization", b"")
        if self.expected is not None and not hmac.compare_digest(supplied, self.expected):
            await _send_json(
                send, 401, {"error": "unauthorized"},
                [(b"www-authenticate", b'Bearer realm="amc-tracker"')],
            )
            return

        await self.app(scope, receive, send)


def build_mcp_app():
    """Starlette app for the MCP endpoint (/mcp). Tries the options available
    in the newer SDK and falls back for older versions."""
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    extra = [h.strip() for h in os.environ.get("AMC_EXTRA_HOSTS", "").split(",") if h.strip()]
    hosts = [h for h in [host, *extra] if h]

    kwargs = {"stateless_http": True}  # tools are stateless; clean across restarts
    if hosts:
        try:
            from mcp.server.transport_security import TransportSecuritySettings
            kwargs["transport_security"] = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[x for h in hosts for x in (h, f"{h}:*")],
                allowed_origins=[f"https://{h}" for h in hosts],
            )
        except ImportError:
            pass

    try:
        return mcp.streamable_http_app(**kwargs)
    except TypeError:
        return mcp.streamable_http_app()


def run_http() -> None:
    import uvicorn

    api_token = os.environ.get("AMC_API_TOKEN", "").strip()
    allow_no_auth = os.environ.get("AMC_ALLOW_NO_AUTH", "0") == "1"

    if api_token and len(api_token) < 24:
        raise RuntimeError("AMC_API_TOKEN must be at least 24 characters.")
    if not api_token and not allow_no_auth:
        raise RuntimeError(
            "No access control configured. Set AMC_API_TOKEN, or set "
            "AMC_ALLOW_NO_AUTH=1 to run without sign-in."
        )
    if not api_token:
        print("WARNING: running WITHOUT authentication. Anyone with the URL can "
              "read and modify all data.", flush=True)

    app = TokenAuthMiddleware(build_mcp_app(), api_token or None)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


# ==========================================================================
# Entry point
# ==========================================================================
if __name__ == "__main__":
    transport = os.environ.get("AMC_TRANSPORT") or ("http" if os.environ.get("PORT") else "stdio")
    if transport.lower() == "stdio":
        mcp.run()
    elif transport.lower() == "http":
        run_http()
    else:
        raise SystemExit("AMC_TRANSPORT must be 'stdio' or 'http'.")