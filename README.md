**Clinker India – Multi‑Tenant Supply Chain Planning SaaS**

- Cloud-native web platform for clinker/cement supply‑chain planning in India.
- Provides tenant-isolated workspaces, OTP-secured onboarding, billing with Razorpay, AI-assisted guidance, and an elastic MILP optimization engine to design transport/production plans.

---

### Table of Contents
- Project Overview
- Key Features
- System Architecture & Stack
- Technical Deep Dive
- Installation & Setup
- Usage Guide
- Environment Variables
- Screens & Flows
- Security & Privacy
- Performance Notes
- Limitations & Known Issues
- Future Enhancements
- Business Value
- Contribution Guide
- Conclusion

---

### Project Overview
- **What it is:** A SaaS web application (Flask) that lets organizations model their clinker supply chain, import structured “nine-sheet” datasets (capacity, demand, logistics, constraints, stocks, costs), and run optimization to minimize cost while meeting service levels.
- **Goal:** Help planners balance production, transport, and inventory with tenant-level governance, billing, and analytics.
- **Audience:** Supply-chain planners, ops managers, data analysts, finance/billing admins, and super admins overseeing all tenants.
- **Scope:** Multi-tenant operations data management, scenario planning, deterministic elastic MILP solver, billing, analytics, and AI chat guidance.

---

### Key Features
- **Core**
  - Multi-tenant isolation with per-org workspaces, datasets, and row-level guards via SQLAlchemy loader criteria.
  - OTP-based auth (registration/login/invite), password resets, and enforced email verification.
  - Scenario management with datasets for IU/GU networks, logistics, constraints, stocks, costs, and demand.
  - Deterministic elastic MILP optimization (PuLP backend) with penalties for unmet fulfillment and stock bounds.
  - Results parsing into KPIs (cost breakdown, service levels, utilization, constraint diagnostics).
- **Major**
  - Billing & seat management (Razorpay orders, signature/webhook verification, GST calculation, trials + paid seats).
  - Super-admin console with cross-tenant analytics, suspension/reactivation, ticket triage, and forced verification.
  - Operations console for plants, routes, inventories, demand, constraints, uploads/downloads, notifications, and activity logs.
  - AI in-product chat (Transfomers Model) with page + org context summarization and safe history trimming.
- **Minor/Hidden**
  - Tenant-scoped CSV import/export for users, invites, datasets, and operations tables.
  - CSRF protection, strong session settings, security headers, and long-lived “remember me” cookies.
  - Seat-capacity enforcement on invites and provisioning; automatic trial bootstrap and workspace/dataset seeding.
  - Support ticket submission with email fan-out to configured support mailbox.
- **Admin/User**
  - Owners/Admins: manage users, seats, invitations, datasets, routes, and optimization runs.
  - Members: view dashboards/analytics, run chat, review scenarios.
  - Super Admin: global analytics, billing visibility, organization status control.

---

### System Architecture & Technology Stack
- **Architecture:** Flask app factory pattern with blueprints (`auth`, `main`, `operations`, `billing`, `tenant`, `superadmin`). SQLAlchemy models with tenant mixin. Service-style optimization pipeline.
- **Frontend:** Server-rendered HTML (Jinja templates), JS widgets (AI chat launcher), CSS theme files under `static/`.
- **Backend:** Flask, Flask-Login, Flask-WTF, Flask-Mail, Flask-Migrate, SQLAlchemy, PuLP (CBC solver), Transfomers Model AI client, Requests.
- **Data Layer:** SQLite by default (or DATABASE_URL). Alembic migrations present under versions.
- **Optimization Flow:** DataMapper → DatasetValidator → ModelBuilderFactory (elastic) → SolverAdapter (PuLP MILP with penalties) → ResultsParser → persisted OptimizationJob/Result.
- **Billing Flow:** Razorpay order creation → client payment → signature verification/webhook → SeatPurchase → OrganizationSubscription update.
- **AI Flow:** JS sends compacted chat + page context to `/api/chat` → Transfomers Model model with safety + output limits → Markdown reply rendered in widget.
- **Tenant Isolation:** `TenantOwnedMixin` plus SQLAlchemy loader criteria registered at app init; per-request org sync in `_register_request_hooks`.

---

### Technical Deep Dive
- **Languages:** Python, JavaScript, HTML, CSS.
- **Key Modules**
  - App factory and tenant hooks in __init__.py.
  - Config & env parsing in config.py; secret key enforcement and secure cookie defaults.
  - ORM models for orgs, users, OTPs, subscriptions, plants, routes, inventories, scenarios, jobs/results in models.py.
  - Auth flows, OTP issuance/validation, invites, password reset in routes.py; forms in forms.py.
  - Super-admin identity and OTP in super_admin.py.
  - Dashboards, analytics, AI chat endpoint, support tickets in routes.py; chat service in chat_service.py.
  - Operations CRUD, CSV/PDF export/import, scenario and optimization orchestration in routes.py with forms in forms.py.
  - Optimization pipeline in optimization: data mapping, validation, MILP build/solve, parsing, and optional robust/stochastic scaffolding.
  - Billing + Razorpay integration in routes.py.
  - Tenant governance helpers in utils.py.
  - Superadmin analytics in routes.py.
  - AI chat widget script in ai-chat.js.
- **Business Logic Highlights**
  - Seat limits combine trial and paid seats; invites/provisioning blocked when over limit.
  - OTP cooldowns, max attempts, and expiry per user; similar controls for super admin OTP.
  - Elastic MILP enforces production caps, trip capacity vs SBQ, min/max closing stock, optional flow constraints, and shortage penalties.
  - Results include service levels, transport cost by mode, utilization, constraint diagnostics, and safety/max-stock violation flags.
- **Dependencies:** See requirements.txt (Flask, SQLAlchemy, PuLP, requests, etc.).

---

### Installation & Setup
- **Prerequisites:** Python 3.11+, pip, SQLite (or external DB), PuLP solver backend (CBC ships with PuLP), internet for Razorpay/Transfomers Model if used.
- **Steps**
  1) Clone repo; `cd Clinker-India`.
  2) `python -m venv .venv && .venv\Scripts\activate` (Windows).
  3) `pip install -r requirements.txt`.
  4) Create .env with required variables (see below).
  5) Initialize DB (for SQLite, auto-created on first run). For migrations: `flask db upgrade` (set `FLASK_APP=run.py`).
  6) Run: `python run.py` (sets up app factory, no reloader).
  7) Open `http://localhost:5000`.
- **Troubleshooting**
  - “SECRET_KEY must be set”: define strong SECRET_KEY.
  - Missing PuLP: `pip install pulp`.
  - Razorpay errors: verify keys and webhook secret.

---

### Usage Guide
- **Auth & Onboarding**
  - Register with org name, admin name, email, password → receive OTP → verify to activate and auto-create trial subscription/workspace.
  - Login with password + optional OTP; resend limited by cooldown.
  - Invite users (single/bulk CSV). Invited users accept via token, set password, verify OTP.
- **Dashboards**
  - View KPIs (users, invites, plants, routes, scenarios), inventory alerts, recent routes/plants/scenarios, and pending invites.
  - Analytics page shows role mix, invite velocity, routes by mode, inventory utilization, scenario costs, job statuses, and notifications.
- **Operations**
  - Manage plants (IU/GU), routes (Road/Rail/Sea), inventories, demands, capacities, costs, opening/closing stocks, constraints, hub stock.
  - Workspaces and datasets: auto-bootstrap defaults; create scenarios with periods and statuses.
  - Upload/download CSVs per table; export reports/optimization outputs.
  - Run optimization: pick scenario, mode (elastic/deterministic), optional limits/penalties → job result stored with plans, KPIs, diagnostics.
- **Billing**
  - View seat status; create Razorpay order for seats; verify payment; webhook fallback; seat counters update subscription status.
- **AI Chat**
  - Open floating chat, type question; widget sends last messages + page context to `/api/chat`; Transfomers Model replies with Markdown guidance.

---

### Environment Variables
- **Core:** `SECRET_KEY` (required), `DATABASE_URL` (optional; defaults to instance/app.db).
- **OTP/Auth:** `OTP_LENGTH`, `OTP_EXPIRY_MINUTES`, `OTP_RESEND_SECONDS`, `OTP_MAX_ATTEMPTS`, `LOGIN_OTP_COOLDOWN_SECONDS`, `PASSWORD_RESET_EXPIRY_MINUTES`, `PASSWORD_RESET_TOKEN_BYTES`.
- **Super Admin:** `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD`, `SUPER_ADMIN_PASSWORD_IS_HASHED`, `SUPER_ADMIN_OTP_LENGTH`, `SUPER_ADMIN_OTP_EXPIRY_MINUTES`, `SUPER_ADMIN_OTP_RESEND_SECONDS`, `SUPER_ADMIN_OTP_MAX_ATTEMPTS`, `SUPER_ADMIN_RATE_LIMIT_PER_MINUTE`.
- **Mail:** `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USE_SSL`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`, `MAIL_SUPPRESS_SEND`, `MAIL_MAX_EMAILS`.
- **Support:** `SUPPORT_ADMIN_EMAIL`.
- **Pricing/Billing:** `DEFAULT_PLAN_CODE`, `PRICING_BASE_AMOUNT_INR`, `PRICING_PER_SEAT_INR`, `GST_RATE`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`.
- **Cookie/Security (prod):** `SESSION_COOKIE_SECURE`, `REMEMBER_COOKIE_SECURE` (set automatically in ProductionConfig).

---

### Screens & Flows
- **Auth Pages:** Register, login (with OTP), verify registration OTP, forgot/reset password, accept invite, bulk invites/provision.
- **Dashboard:** Org KPIs, alerts, routes/plants/scenarios cards, pending invites banner.
- **Analytics:** Charts for users/invites/routes/inventory/scenarios/jobs/notifications/seats.
- **Operations Network:** Tabs for plants, routes, inventory, demand/capacity/costs, logistics, constraints, stocks, scenarios, optimization runs, exports.
- **Billing:** Upgrade/payments with Razorpay checkout; purchase history.
- **Superadmin:** Global dashboard and analytics with revenue, seats, activity, support tickets; controls for org status and user verification.
- **AI Chat Panel:** Slide-over widget with conversation history and Markdown rendering.

---

### Security & Privacy Notes
- CSRF protection on forms and API chat; secure cookies (HttpOnly, SameSite Lax, Secure in production).
- Session/org sync per request; tenant isolation via loader criteria and decorators.
- OTP codes hashed, attempt-limited, and expiration enforced; password reset tokens single-use with expiry.
- Super-admin access gated by env credentials + OTP; global guard on blueprint.
- Razorpay signatures verified for payments/webhooks.
- Mail sending wrapped with exception handling to avoid crashes.
- Recommendation: enforce HTTPS, rotate secrets, harden CSP/Rate limits at reverse proxy, and store secrets outside repo.

---

### Performance & Optimization Notes
- PuLP CBC solver used; runtime limit configurable per job.
- Elastic penalties allow feasible plans even with tight constraints; shortage penalty tunable.
- DataMapper reduces dataset to canonical form; batching constraints grouped to reduce MILP size.
- Analytics queries limited (recent windows, limits) to avoid heavy loads.
- Suggestions: add background worker for long runs, cache analytics aggregates, and pre-validate large CSV uploads asynchronously.

---

### Limitations & Known Issues
- Robust/stochastic solvers are scaffolded; elastic/deterministic path is primary.
- No built-in file storage abstraction for large datasets; uploads processed in-memory.
- CBC solver only; no commercial solver hook yet.
- Limited input validation on some CSV schemas beyond current checks.
- AI chat depends on external Transfomers Model availability and configured key.

---

### Future Enhancements
1) Pluggable solvers (Gurobi/CPLEX) with selectable backends.
2) Async job queue + progress notifications/websocket updates.
3) Rich data lineage and versioning for datasets.
4) Extended constraint types (emissions, budgets, multi-commodity).
5) Audit log exports and SIEM hooks.
6) Fine-grained role/permission model beyond owner/admin/member.
7) UI polish for optimization visualizations (network maps, Sankey flows).

---

### Business Value
- **Who benefits:** Cement/clinker producers, logistics teams, and regional planners needing rapid what-if analysis.
- **Why it matters:** Reduces transport and production cost, improves service levels, enforces governance, and streamlines billing/seating.
- **Vision:** Become a turnkey planning copilot for industrial supply chains with AI-guided insights and automated optimizations.

---

### Contribution Guide
- Fork and branch from `main`; keep changes tenant-safe.
- Add/adjust forms, routes, and models within their blueprints; maintain CSRF and tenant decorators.
- Write migrations for model changes; keep validation in `DatasetValidator`.
- Testing: manual runs of auth/OTP, a sample optimization job, and billing sandbox where applicable.
- Submit PRs with clear description, screenshots for UI, and migration notes.

---

---

### Conclusion
Clinker India delivers a secure, tenant-aware supply-chain planning SaaS with OTP onboarding, billing, analytics, AI guidance, and an elastic MILP optimizer. Configure the environment, load your datasets, and run scenarios to uncover cost-effective, reliable transport and production plans.