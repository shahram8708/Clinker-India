Clinker India — Multi-Period Clinker Allocation & Transport Optimizer
===================================================================

**Project Title and Short Summary**
Clinker India is a production-grade, multi-tenant Flask web application that plans clinker production, allocation, transport, and inventory across Integrated Units (IUs) and Grinding Units (GUs). It couples a MILP optimization engine (PuLP/CBC) with a SaaS UI for tenant onboarding, OTP-secured access, scenario setup, execution (deterministic, stochastic, robust), reporting, and governance.

**Real-World Problem and Why This Exists**
- Clinker must flow from IUs to IUs/GUs over multiple periods via road, rail, or sea, each with trip capacity, minimum batch size (SBQ), integer trip limits, and costs.
- Plants face production caps, safety stock, and inventory limits; demand can be uncertain. Decisions in one period affect later inventory and capacity.
- The goal is to minimize total cost (production, transport, holding, optional shortage penalties) while meeting service levels and respecting operational constraints. Manual planning is error-prone and costly; this tool operationalizes an OR model with guardrails and governance.

**What This Project Does**
- Captures plants, transport routes, inventory, and planning scenarios per tenant organization.
- Runs deterministic, scenario-based stochastic, or robust (worst-case demand uplift) optimizations with integer trips and SBQ coupling.
- Enforces safety stock, inventory capacity, production bounds, trip limits, and optional service-level (chance) constraints.
- Surfaces production, shipment, trips, inventory, and shortage plans with KPIs, CSV exports, PDF reports, notifications, and audit logs.
- Provides multi-tenant authentication, invitation and OTP flows, subscription/seat tracking, and AI-assisted in-app guidance (Transformer Model).

**Key Features (Complete List)**
- Multi-tenant SaaS with org-scoped data via `TenantOwnedMixin` and SQLAlchemy loader criteria.
- Auth flows: email/password, OTP verification, login OTP, password reset, invitation acceptance, and super-admin OTP login.
- Roles: owner, admin, member; admin-only CRUD for plants/routes/inventory/scenarios; super-admin bypass.
- Billing primitives: pricing plans, seat purchases, trial vs paid states, seat limits, Razorpay fields; subscription guard on seat allocation.
- Operations console: create/edit plants (IU/GU), routes (mode, capacity, SBQ, trip caps, costs), inventory, scenarios; filter tables; inline edits.
- Optimization engine: deterministic MILP; stochastic extensive-form; robust worst-case with stress scenarios and heuristics fallback; runtime limit support.
- Constraint realism: integer trips, SBQ lower bounds, trip capacity, production caps, safety stock, inventory caps, optional shortage with penalty and service-level cap.
- Outputs: KPI block (costs, service level, utilization, safety gaps), production/shipment/trips/inventory/shortage plans, diagnostics, comparison of deterministic vs uncertain runs.
- Exports and reporting: CSV datasets (plants, routes, inventory, scenarios); PDF reports (inventory health or transport network) with styled cards and highlights.
- Notifications and activity log: user-scoped inbox, severity levels, filters, audit trail for CRUD and runs.
- AI chat endpoint (`/api/chat`) backed by Transformer Model with page-context grounding and safety trims.
- Support contact form with category tagging and mail-out to support admin.

**Who This Project Is For / Target Users**
- Supply chain and logistics teams planning clinker flows across IUs and GUs.
- Operations researchers and data engineers needing a reference MILP with SBQ and integer trips plus uncertainty handling.
- Product teams building SaaS-style industrial optimization apps with tenant isolation and governance.

**High-Level Overview (Non-Technical)**
- Users create an organization, invite teammates, and set up plants, routes, and inventories.
- They define scenarios (number of periods, optional demand profile) and run an optimizer that proposes production and transport plans.
- Results show costs, trips, shipments, inventories, and risk/service metrics, and can be exported as CSV/PDF.
- Notifications, activity logs, and AI help text keep users informed; support requests go to admins by email.

**Detailed System Explanation (Technical)**
- Flask app factory wires blueprints for auth, billing, main dashboard, operations, tenant utilities, and superadmin.
- Tenant isolation is enforced via SQLAlchemy loader criteria and per-request session checks; non-superadmin queries auto-filter by `organization_id`.
- Optimization pipeline: DataMapper loads active plants/routes/inventory/demand into a `CanonicalDataset`; DatasetValidator guards connectivity, demand, and capacity; ModelBuilder chooses deterministic/stochastic/robust definitions; SolverAdapter (PuLP/CBC) solves; ResultsParser computes KPIs; OptimizationResult persists plans/KPIs.
- Stochastic mode builds scenario sets with probabilities; robust mode stresses demand via uplift multipliers and includes fallback tiers (relax routes, simplified aggregation, heuristic backup) to recover feasible plans.
- Auth flow issues OTPs, hashes codes, rate-limits resends, and guards sessions; super-admin has a separate identity and OTP path.
- Reporting uses ReportLab to render PDF summaries; CSV export is available for key datasets.
- AI chat wraps `Transformer Model` with message sanitization and page-context injection.

**Architecture / System Design**
- Flask application factory initializes config, logging, extensions (SQLAlchemy, Flask-Migrate, LoginManager, CSRF, Mail), and blueprints.
- Blueprints: auth (register/login/OTP/invite/reset), billing (seat purchases), main (dashboard, chat, static pages, support), operations (network CRUD, optimization, exports, activity, notifications), tenant (guards), superadmin (privileged routes).
- Persistence: SQLAlchemy models for organizations, users, OTPs, invitations, pricing/subscriptions/seat purchases, plants, routes, inventory, scenarios, optimization jobs/results, logs, notifications, contact requests.
- Optimization stack: DataMapper → ModelBuilderFactory → SolverAdapter (PuLP) → ResultsParser; ScenarioManager for stochastic/robust sets; RobustSolver with pre-checks and fallbacks.
- Frontend: Jinja templates, WTForms, static assets (CSS/JS), with contextual AI guidance.

**Technology Stack**
- Python, Flask, SQLAlchemy, Flask-Login, Flask-WTF, Flask-Mail, Flask-Migrate
- Optimization: PuLP with CBC solver backend
- Reporting: ReportLab for PDFs; CSV via Python stdlib
- AI: Transformer Model (Transformer Model)
- Database: SQLite by default (override via `DATABASE_URL`); Alembic migrations

**How the System Works (Step-by-Step Flow)**
1) User authenticates (registration OTP, login OTP, or super-admin OTP) and tenant context is synced per request.
2) User creates plants (IU/GU), routes (mode, SBQ, capacity, trips, cost), and inventory snapshots; scenarios capture period count and optional demand profile.
3) User submits an optimization run with mode, runtime, shortage policy, service-level target, scenario samples, or demand uplift.
4) Engine loads canonical data, validates feasibility, builds the model, and invokes SolverAdapter (deterministic/stochastic/robust path).
5) Solver returns plans and costs; ResultsParser derives KPIs (service level, utilization, costs, safety gaps, trips/shipments by route/destination).
6) Results are persisted, surfaced on the UI, compared against deterministic baseline for uncertainty modes, and available for CSV/PDF export.
7) ActivityLog and Notifications record the run; users can download reports or continue iterating.

**Algorithms / Logic / Technical Concepts**
- Deterministic MILP with decision variables: production `X`, shipments `Ship`, trips `Trips` (integer), inventory `Inv`, optional shortage `Shortage`.
- Constraints: production caps at IUs; zero production at GUs; inventory balance per plant-period; safety stock lower bound; max inventory capacity; shipment bound by trip capacity and SBQ; integer trip limits; optional chance-style cap on total shortage.
- Objective: minimize production + transport (per-trip and per-ton) + holding + optional shortage penalties.
- Stochastic: extensive-form model with shared first-stage variables; scenarios built from demand multipliers with normalized probabilities; reliability score via weighted service level and chance constraint check.
- Robust: demand uplift scenarios; min-max formulation or fallback tiers (soft relaxation, simplified aggregation, heuristic backup) with pre-solve feasibility screens (supply vs demand, connectivity, inventory logic, integer traps).
- Diagnostics: prechecks, solver status, MIP gap, shortage totals, validation issues embedded in KPIs.

**Modules / Components Breakdown**
- App setup: [app/__init__.py](app/__init__.py) configures app, logging, extensions, blueprints, and error handlers.
- Config: [app/config.py](app/config.py) environment-driven settings for security, mail, OTP, billing, and DB.
- Models: [app/models.py](app/models.py) defines all domain entities with constraints and helper methods.
- Auth: [app/auth/routes.py](app/auth/routes.py) handles register/login/OTP/reset/invite/bulk invite/provision; super-admin helpers in [app/auth/super_admin.py](app/auth/super_admin.py).
- Tenant isolation: [app/tenant/utils.py](app/tenant/utils.py) enforces org scoping via loader criteria and decorators.
- Dashboard and AI: [app/main/routes.py](app/main/routes.py) for dashboard metrics, invite acceptance, pages, support; AI chat in [app/main/chat_service.py](app/main/chat_service.py).
- Operations: [app/operations/routes.py](app/operations/routes.py) for CRUD, filters, optimization runs, exports, PDFs, notifications, activity log.
- Optimization core: Data mapping [app/optimization/data_mapper.py](app/optimization/data_mapper.py); model builders [app/optimization/model_builder.py](app/optimization/model_builder.py); solvers [app/optimization/deterministic_solver.py](app/optimization/deterministic_solver.py), [app/optimization/stochastic_solver.py](app/optimization/stochastic_solver.py), [app/optimization/robust_solver.py](app/optimization/robust_solver.py); adapter [app/optimization/solver_adapter.py](app/optimization/solver_adapter.py); scenario management [app/optimization/scenario_manager.py](app/optimization/scenario_manager.py); validation [app/optimization/validators.py](app/optimization/validators.py); parsing [app/optimization/results_parser.py](app/optimization/results_parser.py).
- Entrypoint: [run.py](run.py) starts the app without the Flask reloader.

**Backend / Frontend / Database**
- Backend: Flask blueprints with ORM models; CBC solver via PuLP; mail via Flask-Mail.
- Frontend: Jinja templates, WTForms, static CSS/JS (including `ai-chat.js` and button spinners), PDF styling via ReportLab.
- Database: SQLite default under `instance/app.db`; Alembic migrations stored in [migrations/](migrations/).

**Data Models / Entities (selected)**
- `Organization`, `OrganizationSubscription`, `PricingPlan`, `SeatPurchase`
- `User` (role, lifecycle, verification), `EmailOTP`, `PasswordResetToken`, `SuperAdminOTP`, `UserInvitation`
- `Plant` (IU/GU, capacities, costs, safety stock), `TransportRoute` (mode, capacity, SBQ, trips, costs), `Inventory`
- `PlanningScenario` (periods, status, summary/demand), `OptimizationJob`, `OptimizationResult`
- `ActivityLog`, `Notification`, `ContactRequest`

**APIs / Endpoints (representative)**
- Auth: `/auth/register`, `/auth/login`, `/auth/logout`, `/auth/verify/registration`, `/auth/forgot-password`, `/auth/reset-password`, `/auth/invite`, `/auth/bulk-invite`, `/auth/provision`
- Dashboard: `/dashboard`, `/invitations/<id>/accept-self`, `/api/chat`
- Operations: `/ops/network` (CRUD + forms), `/ops/optimization/run`, `/ops/exports/csv`, `/ops/exports/pdf`, `/ops/activity`, `/ops/notifications`
- Static pages/support: `/about`, `/terms`, `/privacy`, `/contact-support`

**Security Measures and Authentication**
- OTP for registration, login, invites, and super-admin; hashed codes, max attempts, resend cooldowns, expiry times.
- CSRF protection via Flask-WTF; security headers added after each request.
- Session hardening: HTTPOnly cookies, strong session protection, optional Secure/SameSite flags.
- Tenant enforcement: per-request org validation and SQLAlchemy loader criteria to prevent cross-org access.
- Password reset tokens with split identifier/secret, single-use invalidation, expiry, and IP/UA logging.

**Performance and Scalability Discussion**
- CBC via PuLP is suitable for moderate networks; pluggable solver adapter allows upgrading to commercial solvers.
- Stochastic extensive-form scales with scenario count and periods; scenario_samples should be constrained.
- Robust solver includes relaxed and aggregated fallbacks to recover feasibility; demand uplift configurable.
- DataMapper filters to active plants/routes to reduce model size; inventory/demand defaults guard sparse data.

**Input Requirements / Data Needed**
- Plants: name, type (IU/GU), location, production capacity/cost, holding cost, consumption capacity, max inventory, safety stock, status.
- Routes: source plant, destination plant, mode (Road/Rail/Sea), trip capacity, SBQ, max trips/period, cost per trip/ton, lead time (optional), status.
- Inventory: current inventory per plant.
- Scenarios: scenario name, periods, optional demand profile or embedded demand in summary.
- Run options: mode, runtime limit, demand uplift %, scenario samples, allow_shortage flag, shortage_penalty, service_level_target, mark scenario completed.

**Outputs / Results / KPIs**
- Cost breakdown: production, transport, holding, shortage; worst-case cost for robust; comparison vs deterministic for uncertainty modes.
- Plans: production per IU-period; shipments per route-period; trips per route-period; inventory per plant-period; shortages per plant-period (if enabled).
- KPIs: service level %, reliability score, risk exposure, production/transport utilization, total shipped/trips/produced, inventory slack, safety stock gaps, trips by route, shipments by destination, validation warnings.
- Exports: CSV datasets; PDF reports for inventory health or transport network; structured storage payload for robust runs.

**Installation Guide (Step-by-Step)**
1) Clone: `git clone https://github.com/shahram8708/Clinker-India` and `cd Clinker-India`.
2) Create and activate a Python virtual environment.
3) Install dependencies: `pip install -r requirements.txt`.
4) Copy environment variables into `.env` (no sample file is provided); set at least `SECRET_KEY`.
5) Initialize the database: `flask db upgrade` (set `FLASK_APP=run.py` if needed).
6) (Optional) Create `instance/` directory if not auto-created.

**Environment Variables and Configuration**
- Required: `SECRET_KEY`.
- Database: `DATABASE_URL` (defaults to SQLite under instance/app.db, auto-normalized on Windows paths).
- Mail/OTP: `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`, `MAIL_USE_TLS`, `MAIL_USE_SSL`, `MAIL_SUPPRESS_SEND`, `MAIL_MAX_EMAILS`, OTP settings (`OTP_LENGTH`, `OTP_EXPIRY_MINUTES`, `OTP_RESEND_SECONDS`, `OTP_MAX_ATTEMPTS`, `LOGIN_OTP_COOLDOWN_SECONDS`, `PASSWORD_RESET_EXPIRY_MINUTES`, `PASSWORD_RESET_TOKEN_BYTES`).
- Super admin: `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD`, `SUPER_ADMIN_PASSWORD_IS_HASHED`, `SUPER_ADMIN_OTP_LENGTH`, `SUPER_ADMIN_OTP_EXPIRY_MINUTES`, `SUPER_ADMIN_OTP_RESEND_SECONDS`, `SUPER_ADMIN_OTP_MAX_ATTEMPTS`, `SUPER_ADMIN_RATE_LIMIT_PER_MINUTE`.
- Billing: `DEFAULT_PLAN_CODE`, `PRICING_BASE_AMOUNT_INR`, `PRICING_PER_SEAT_INR`, `GST_RATE`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`.
- Security cookies: `SESSION_COOKIE_HTTPONLY`, `REMEMBER_COOKIE_HTTPONLY`, `SESSION_COOKIE_SAMESITE`, `REMEMBER_COOKIE_SAMESITE`, enable `SESSION_COOKIE_SECURE` and `REMEMBER_COOKIE_SECURE` in production.

**Running Locally (Development Mode)**
- Set `FLASK_ENV=development` (default). Run `flask run` or `python run.py` (reloader disabled in `run.py`).
- Default database is SQLite; instance folder is created on start if missing.

**Running in Production**
- Set `FLASK_ENV=production`; provide strong `SECRET_KEY` and production `DATABASE_URL`.
- Serve via Gunicorn/Uvicorn or equivalent: `gunicorn -w 4 -b 0.0.0.0:8000 run:app` behind a reverse proxy with TLS termination and secure cookies enabled.
- Run `flask db upgrade` on deploy; ensure instance directory is writable and mail credentials are valid.

**Usage Guide (Step-by-Step)**
1) Register org admin, verify email via OTP, and sign in.
2) Add plants (IU/GU) with capacities, costs, and safety stock; add transport routes with mode, SBQ, capacity, trip caps, and costs; record inventory levels.
3) Create planning scenarios (periods, optional demand). Demand defaults to GU consumption if not provided.
4) From `/ops/network`, configure an optimization run: choose mode, runtime limit, shortage policy, penalty, service-level target, scenario samples, demand uplift, and mark scenario completion if desired.
5) Run optimization, review KPIs and plans; download CSV/PDF; mark scenarios executed/completed.
6) Manage invites/users, monitor notifications and activity log, request support, and use AI chat for guidance.

**Examples / Demo Scenarios**
- Deterministic: mode `deterministic`, allow_shortage false, shortage_penalty unused, service_level_target optional.
- Stochastic: mode `stochastic`, scenario_samples=3, allow_shortage true, shortage_penalty set, service_level_target=0.95.
- Robust: mode `robust`, demand_uplift_pct=0.1 to hedge 10 percent demand growth; compare worst-case vs deterministic in KPIs.

**Deployment Guide**
- Build/collect env vars, run migrations, and start via Gunicorn as above.
- Place behind a reverse proxy (Nginx/ALB) with TLS; enable Secure cookies; configure health checks on `/` or `/dashboard`.
- Rotate `SECRET_KEY` and mail credentials securely; ensure `instance/` is writable for SQLite.

**Troubleshooting Guide**
- Optimization fails precheck: check supply vs demand, missing inbound routes for GUs, SBQ > trip capacity, or inventory exceeding capacity; enable shortage or relax SBQ/trip caps.
- Seat allocation blocked: subscription status may be `subscription_required` or seats exhausted; purchase seats or update billing config.
- Emails not sent: verify mail server/ports/credentials; disable `MAIL_SUPPRESS_SEND`.
- SQLite locking in multi-process: migrate to Postgres/MySQL via `DATABASE_URL`.

**Error Handling and Limitations**
- Missing or weak `SECRET_KEY` raises at startup.
- If supply plus initial inventory is below demand and shortage not allowed, solver precheck returns infeasible.
- CBC solver limits scale for very large instances; integer trip constraints may increase solve time.
- AI chat returns 503 if key missing; errors are logged with safe messages to users.
- Robust solver flags integer traps (e.g., max_trips=0) and safety-capacity conflicts.

**Security Notes / Best Practices**
- Keep `SECRET_KEY` secret and rotate periodically.
- Enable HTTPS, `SESSION_COOKIE_SECURE`, `REMEMBER_COOKIE_SECURE`, and strict SameSite in production.
- Limit OTP resend rates and monitor super-admin OTP usage; prefer hashed super-admin password in env (`SUPER_ADMIN_PASSWORD_IS_HASHED=true`).
- Enforce least privilege: owner/admin for CRUD; members read-only.
- Keep dependencies patched; run `pip install -r requirements.txt` after updates.

**Performance Tips**
- Limit periods and scenario_samples to keep MILP tractable; set `runtime_limit` when invoking solver.
- Prune inactive plants/routes; ensure SBQ <= trip capacity to avoid traps.
- Use demand aggregation in robust runs when scale grows; consider switching solver adapter for large instances.

**Roadmap / Future Enhancements**
- Cement-stage expansion and carbon constraints.
- Faster solver plugins (e.g., Gurobi) via SolverAdapter abstraction.
- Richer uncertainty modeling, dynamic scenario generation, and visualization of routes/flows.
- Deeper dashboards, maps, and live ERP/production system integrations.

**Why This Project Is Powerful / Impactful**
- Combines realistic transport and inventory constraints with uncertainty handling for actionable clinker plans.
- Delivers SaaS-ready governance (tenancy, roles, billing, auditability) plus AI assistance, making it deployable in industrial contexts.
- Produces exportable plans and KPIs that align with operational constraints (integer trips, SBQ, safety stock).

**Contribution Guidelines**
- Fork the repository, create feature branches, and open pull requests.
- Add tests where possible; keep tenant isolation and security constraints intact.
- Coordinate schema changes with Alembic migrations under `migrations/`.

**Credits / Authors**
- Built by Shah Ram and the Clinker India engineering team; thanks to the open-source ecosystem (Flask, SQLAlchemy, PuLP, CBC, ReportLab, Transformer Model).

**License**
- License file not provided. Add a LICENSE file to declare terms before distribution.

**FAQ**
- Can shortages be disallowed? Yes; set allow_shortage false (default) to force strict service, or true with penalties and service-level target.
- Can GUs produce clinker? No; constraints set GU production to zero.
- What happens if safety stock exceeds capacity? Validators raise errors; robust prechecks surface warnings.
- How are tenants isolated? `TenantOwnedMixin` plus SQLAlchemy loader criteria and per-request session guards enforce organization scoping.
