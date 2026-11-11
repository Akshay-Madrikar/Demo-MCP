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
  5. Interactive Browser Dashboard + Live JSON Editor (completely open, no tokens)

STORAGE
  No database. Data is kept in Python dicts and saved to a JSON file after
  EVERY change (atomic write), so nothing is lost on restart.
  Default file: amc_data.json next to this script (override: AMC_DATA_FILE).
"""

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
    global _next_amc_id, _next_service_id
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            AMCS.clear()
            SERVICES.clear()
            AMCS.update({int(k): v for k, v in raw.get("amcs", {}).items()})
            SERVICES.update({int(k): v for k, v in raw.get("services", {}).items()})
            for k in PERSISTED_CONFIG_KEYS:
                if k in raw.get("config", {}):
                    CONFIG[k] = raw["config"][k]
            _next_amc_id = raw.get("next_amc_id", 1)
            _next_service_id = raw.get("next_service_id", 1)
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            raise RuntimeError(f"Could not read data file {DATA_FILE!r}: {exc}") from exc
    elif LOAD_SAMPLE_DATA:
        _load_sample_data()

    _next_amc_id = max(_next_amc_id, max(AMCS, default=0) + 1)
    _next_service_id = max(_next_service_id, max(SERVICES, default=0) + 1)
    if not os.path.exists(DATA_FILE):
        save_data()


load_data()

mcp = MCPServer(f"{CONFIG['business_name']} - AMC Tracker")


# ==========================================================================
# Helpers & Tools
# ==========================================================================
def parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ValueError(f"{field} must be in YYYY-MM-DD format, got {value!r}")


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def contract_year(start: date, on: date) -> int:
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


@mcp.tool()
def get_business_config() -> dict:
    return {**CONFIG, "data_file": DATA_FILE}


@mcp.tool()
def update_business_config(
    business_name: Optional[str] = None,
    currency: Optional[str] = None,
    default_free_services_per_year: Optional[int] = None,
) -> dict:
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


@mcp.tool()
def record_service(
    amc_id: int,
    service_date: Optional[str] = None,
    performed_by: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
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
    d = amc_view(get_amc_or_fail(amc_id), detail=True)
    return {
        "amc_id": amc_id,
        "customer_name": d["customer_name"],
        "free_services": d["free_services"],
        "services": d["services"],
    }


@mcp.tool()
def delete_service(service_id: int) -> dict:
    with _lock:
        svc = SERVICES.pop(service_id, None)
        if not svc:
            raise ValueError(f"Service #{service_id} not found.")
        reassign_services(AMCS[svc["amc_id"]])
        save_data()
        return {"deleted": service_id, "amc_id": svc["amc_id"]}


@mcp.resource("amc://{amc_id}")
def amc_resource(amc_id: str) -> str:
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
# HTTP Serving & Interactive Browser Dashboard (Open Access - No Tokens)
# ==========================================================================
async def _send_json(send, status: int, body: dict, extra_headers=None) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json; charset=utf-8"),
               (b"content-length", str(len(payload)).encode())]
    headers += extra_headers or []
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


async def handle_dashboard(send):
    """HTML Web Viewer & JSON Editor for Browser."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AMC JSON Manager & Live Viewer</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 1px solid #334155; padding-bottom: 16px; }
    h1 { margin: 0; color: #38bdf8; font-size: 24px; }
    .meta { color: #94a3b8; font-size: 14px; margin-top: 4px; }
    .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
    .tab-btn { background: #1e293b; color: #94a3b8; border: 1px solid #334155; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: 600; }
    .tab-btn.active { background: #0284c7; color: white; border-color: #0284c7; }
    .actions { display: flex; gap: 10px; }
    .btn { background: #0284c7; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 14px; }
    .btn:hover { background: #0369a1; }
    .btn-save { background: #16a34a; }
    .btn-save:hover { background: #15803d; }
    .alert { padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; display: none; font-size: 14px; }
    .alert-success { background: #14532d; color: #4ade80; border: 1px solid #166534; }
    .alert-error { background: #450a0a; color: #f87171; border: 1px solid #991b1b; }
    pre, textarea { width: 100%; box-sizing: border-box; background: #1e293b; padding: 16px; border-radius: 8px; border: 1px solid #334155; font-family: "Courier New", monospace; font-size: 14px; color: #e2e8f0; line-height: 1.5; min-height: 500px; }
    textarea { resize: vertical; outline: none; }
    textarea:focus { border-color: #38bdf8; }
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>AMC Tracker JSON Manager</h1>
      <div class="meta" id="status-meta">Connected to live server (Public Access)</div>
    </div>
    <div class="actions">
      <button class="btn" onclick="fetchData()">Refresh</button>
      <button class="btn btn-save" onclick="saveData()">Save Changes</button>
    </div>
  </div>

  <div id="alert" class="alert"></div>

  <div class="tabs">
    <button class="tab-btn active" id="tab-view-btn" onclick="switchTab('view')">Live View</button>
    <button class="tab-btn" id="tab-edit-btn" onclick="switchTab('edit')">JSON Editor</button>
  </div>

  <div id="view-mode">
    <pre id="json-viewer">Loading JSON file...</pre>
  </div>

  <div id="edit-mode" style="display: none;">
    <textarea id="json-editor" placeholder="Paste or edit JSON content here..."></textarea>
  </div>

  <script>
    let activeTab = 'view';

    function showAlert(msg, isError = false) {
      const el = document.getElementById('alert');
      el.textContent = msg;
      el.className = 'alert ' + (isError ? 'alert-error' : 'alert-success');
      el.style.display = 'block';
      setTimeout(() => { el.style.display = 'none'; }, 5000);
    }

    function switchTab(tab) {
      activeTab = tab;
      document.getElementById('tab-view-btn').className = 'tab-btn ' + (tab === 'view' ? 'active' : '');
      document.getElementById('tab-edit-btn').className = 'tab-btn ' + (tab === 'edit' ? 'active' : '');
      document.getElementById('view-mode').style.display = tab === 'view' ? 'block' : 'none';
      document.getElementById('edit-mode').style.display = tab === 'edit' ? 'block' : 'none';
      if (tab === 'edit') {
        document.getElementById('json-editor').value = document.getElementById('json-viewer').textContent;
      }
    }

    async function fetchData() {
      try {
        const res = await fetch('/api/data');
        if (!res.ok) throw new Error('HTTP status ' + res.status);
        const data = await res.json();
        const formatted = JSON.stringify(data, null, 2);
        document.getElementById('json-viewer').textContent = formatted;
        if (activeTab === 'edit' && document.activeElement !== document.getElementById('json-editor')) {
          document.getElementById('json-editor').value = formatted;
        }
        document.getElementById('status-meta').textContent = 'Last synced: ' + new Date().toLocaleTimeString();
      } catch (err) {
        document.getElementById('status-meta').textContent = 'Error: ' + err.message;
        showAlert('Failed to fetch JSON data: ' + err.message, true);
      }
    }

    async function saveData() {
      const rawText = activeTab === 'edit' 
        ? document.getElementById('json-editor').value 
        : document.getElementById('json-viewer').textContent;

      let parsed;
      try {
        parsed = JSON.parse(rawText);
      } catch (err) {
        showAlert('Invalid JSON syntax: ' + err.message, true);
        return;
      }

      try {
        const res = await fetch('/api/data', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(parsed)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || 'Failed to update');
        
        showAlert('JSON file updated successfully!');
        fetchData();
      } catch (err) {
        showAlert('Save error: ' + err.message, true);
      }
    }

    fetchData();
    setInterval(() => { if (activeTab === 'view') fetchData(); }, 4000);
  </script>
</body>
</html>"""
    payload = html_content.encode("utf-8")
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(payload)).encode())
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


async def handle_api_data(scope, receive, send):
    method = scope.get("method", "GET").upper()

    if method == "GET":
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            await _send_json(send, 200, raw)
        except Exception as exc:
            await _send_json(send, 500, {"error": str(exc)})
        return

    if method == "POST":
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break

        try:
            new_data = json.loads(body.decode("utf-8"))
            if not isinstance(new_data, dict) or "amcs" not in new_data or "services" not in new_data:
                await _send_json(send, 400, {"error": "Invalid format: payload must include 'amcs' and 'services' keys."})
                return

            with _lock:
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(new_data, f, indent=2, ensure_ascii=False)
                load_data()

            await _send_json(send, 200, {"status": "ok", "message": "File updated and reloaded."})
        except json.JSONDecodeError as err:
            await _send_json(send, 400, {"error": f"JSON Decode Error: {str(err)}"})
        except Exception as exc:
            await _send_json(send, 500, {"error": str(exc)})


class OpenAccessMiddleware:
    """Pure ASGI middleware allowing completely public access without any tokens."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Normalize path by removing trailing slashes
        path = scope.get("path", "").rstrip("/")

        if path == "/health":
            await _send_json(send, 200, {"status": "ok"})
            return
        if path in ("", "/dashboard"):
            await handle_dashboard(send)
            return
        if path == "/api/data":
            await handle_api_data(scope, receive, send)
            return

        await self.app(scope, receive, send)


def build_mcp_app():
    kwargs = {"stateless_http": True}
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    except ImportError:
        pass

    try:
        return mcp.streamable_http_app(**kwargs)
    except TypeError:
        return mcp.streamable_http_app()


def run_http() -> None:
    import uvicorn

    app = OpenAccessMiddleware(build_mcp_app())
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    transport = os.environ.get("AMC_TRANSPORT") or ("http" if os.environ.get("PORT") else "stdio")
    if transport.lower() == "stdio":
        mcp.run()
    elif transport.lower() == "http":
        run_http()
    else:
        raise SystemExit("AMC_TRANSPORT must be 'stdio' or 'http'.")