# AssetFlow — Enterprise Asset & Resource Management System

## Setup (takes ~1 minute)

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:5000** in your browser. The database (`assetflow.db`)
and demo data are created automatically on first run.

## Demo accounts

| Role          | Email                     | Password    |
|---------------|---------------------------|-------------|
| Admin         | admin@assetflow.com       | admin123    |
| Asset Manager | manager@assetflow.com     | manager123  |
| Employee      | alice@assetflow.com       | password123 |
| Employee      | bob@assetflow.com         | password123 |
| Employee      | carol@assetflow.com       | password123 |

Signup (`/signup`) always creates a plain Employee account — only an Admin/Asset
Manager can allocate assets, matching the "no self-elevating roles" requirement.

## Core features implemented

- **Auth**: login/signup, session-based, Employee-only signup
- **Dashboard**: live KPI cards (Available, Allocated, Overdue, Active Bookings)
- **Asset Registry**: register assets with auto-generated tag (AF-0001...), search/filter
- **Allocation**: allocate to employee, blocks double-allocation with a clear
  "currently held by X" message, return flow
- **Resource Booking**: time-slot booking for shared/bookable assets with
  strict overlap validation (adjacent slots allowed, overlapping slots rejected)

## Two unique differentiators (not in the base spec)

1. **Asset Trust Score** — every employee gets a live-computed reputation score
   (0–100) based on their return history (on-time vs. late vs. currently overdue).
   Shown right on the Allocate screen so managers can make informed decisions
   before handing out high-value assets. See `compute_trust_score()` in `app.py`.

2. **QR-Code Self-Service Lookup** — every asset detail page shows an
   auto-generated QR code. Scanning it opens a public page (`/lookup/<tag>`,
   no login required) showing live status/holder — turning a printed sticker
   on any piece of equipment into an instant status check.

## Project structure

```
assetflow/
├── app.py                 # all routes + business logic
├── requirements.txt
├── templates/              # Jinja2 templates (Bootstrap 5 styled)
│   ├── base.html
│   ├── login.html / signup.html
│   ├── dashboard.html
│   ├── assets.html / asset_detail.html
│   ├── allocate.html
│   ├── bookings.html
│   └── lookup.html         # public QR landing page
└── static/qr/               # auto-generated QR images (created at runtime)
```

