# CLINKER SUPPLY CHAIN OPTIMIZATION - Complete Analysis & Implementation Guide

**Project Context:** Smart India Hackathon (SIH) 2025 | 36-hour Hackathon | LJ University Ahmedabad

**Date:** January 2026

---

## 📋 TABLE OF CONTENTS

1. Problem Understanding
2. Data Input Structure (From Excel)
3. System Architecture & Flow
4. Implementation Roadmap
5. Minimum Viable Product (MVP) Requirements
6. Database Schema Design
7. Frontend Form Specifications
8. Backend Processing Pipeline
9. Optimization Model Overview
10. Deliverables Checklist

---

## 1. PROBLEM UNDERSTANDING

### What is the Problem?

**Industry Context:**
- Cement manufacturing involves producing **clinker** at **Integrated Units (IUs)** - large plants that produce clinker as intermediate product
- Clinker is consumed at both **IUs** (own use) and **Grinding Units (GUs)** - smaller facilities that grind clinker into cement
- Network involves **20 Integrated Units** and **25+ Grinding Units** across multiple regions

### Why is it Complex?

1. **Multi-plant Network:** 45+ facilities, multiple production & consumption points
2. **Multiple Transportation Modes:** T1 (Road direct), T2 (Rail/Ship bulk), etc., each with:
   - Different freight costs
   - Different handling costs
   - Quantity multipliers (1 ton in T1 vs 3000 tons in T2)
   - Minimum batch quantities
3. **Multi-period Planning:** 3 months of data (M1, M2, M3) with period-dependent constraints
4. **Inventory Dynamics:** Opening stock, closing stock requirements, safety stock constraints
5. **Integer Constraints:** Transportation must use integer number of trips
6. **Cost Minimization:** Balance between:
   - Production costs (vary by IU & time)
   - Transportation costs (vary by mode, route, time)
   - Inventory holding costs (implicit - minimize excess)

### What are We Solving For?

**Objective:** Minimize total logistics cost (production + transportation + inventory)

**Subject to:**
- Every GU demand must be fulfilled (or MIN_FULFILLMENT % if specified)
- Every IU internal demand must be fulfilled
- Cannot exceed production capacity at any IU
- Cannot exceed transportation capacity on any route
- Must maintain safety stock (MIN CLOSE STOCK) at all locations each period
- Cannot exceed maximum inventory at any location
- Transportation uses integer trips

---

## 2. DATA INPUT STRUCTURE - EXACT FROM EXCEL

### Sheet 1: ClinkerDemand
**Purpose:** Define what each plant needs

**Structure:**
```
IUGU CODE      | TIME PERIOD | DEMAND    | MIN FULFILLMENT (%)
IU_003         | 1           | 222553    | [blank]
GU_002         | 1           | 161885    | [blank]
```

**Key Points:**
- **IUGU CODE:** Either IU_XXX (Integrated Unit) or GU_XXX (Grinding Unit)
- **TIME PERIOD:** 1, 2, or 3 (representing months M1, M2, M3)
- **DEMAND:** Tons of clinker required (integer or decimal)
- **MIN FULFILLMENT (%):** If blank = 100% must be fulfilled; if value = that percentage minimum

**Data in File:**
- 44 IUs + 26 GUs = 70 plants
- 3 time periods
- Total rows: ~210 demand records
- Range: Demands vary from 51,215 tons to 294,543 tons per period

**Excel Format Requirements for User Input:**
```
Column A: IUGU CODE (validation: must match plant list)
Column B: TIME PERIOD (validation: 1-3)
Column C: DEMAND (validation: > 0, numeric)
Column D: MIN FULFILLMENT (%) (validation: 0-100, optional)
```

---

### Sheet 2: ClinkerCapacity
**Purpose:** Define maximum production at each IU

**Structure:**
```
IU CODE  | TIME PERIOD | CAPACITY
IU_002   | 1           | 288751
IU_001   | 1           | 300410
```

**Key Points:**
- **IU CODE:** Only Integrated Units (IU_001 to IU_021)
- **TIME PERIOD:** 1, 2, or 3
- **CAPACITY:** Maximum tons that IU can produce in that period
- **Default Rule:** If capacity not given → IU capacity = 0 (cannot produce)

**Data in File:**
- 20 IUs × 3 periods = 60 capacity records
- Range: 39,255 to 390,059 tons per period
- Some IUs have lower capacity in period 2 (e.g., IU_015: 390,059 → 39,255 tons)

**Excel Format Requirements for User Input:**
```
Column A: IU CODE (validation: IU_XXX only, 1-21)
Column B: TIME PERIOD (validation: 1-3)
Column C: CAPACITY (validation: >= 0, numeric)
```

---

### Sheet 3: ProductionCost
**Purpose:** Cost per ton to manufacture clinker at each IU

**Structure:**
```
IU CODE  | TIME PERIOD | PRODUCTION COST (₹/ton)
IU_020   | 1           | 1914
IU_005   | 1           | 1487
```

**Key Points:**
- **IU CODE:** Only Integrated Units
- **TIME PERIOD:** 1, 2, or 3
- **PRODUCTION COST:** ₹ per ton (₹1,414 to ₹2,275/ton)
- **Default Rule:** If cost missing → Very high penalty (10,000 ₹/ton) applied to force optimization away

**Data in File:**
- 20 IUs × 3 periods = 60 cost records
- Range: ₹1,410 to ₹2,276 per ton
- Costs vary period-to-period (reflecting seasonal/market variations)

**Excel Format Requirements for User Input:**
```
Column A: IU CODE (validation: IU_XXX only)
Column B: TIME PERIOD (validation: 1-3)
Column C: PRODUCTION COST (validation: > 0, numeric, max 5 digits)
```

---

### Sheet 4: LogisticsIUGU
**Purpose:** Transportation cost structure for each route & mode

**Structure:**
```
FROM IU CODE | TO IUGU CODE | TRANSPORT CODE | TIME PERIOD | FREIGHT COST | HANDLING COST | QUANTITY MULTIPLIER
IU_002       | IU_002       | T1             | 1           | 0.0          | 0             | 1
IU_002       | GU_004       | T1             | 1           | 955.8        | 0             | 1
IU_002       | GU_008       | T2             | 1           | 1430.164     | 0             | 3000
```

**Key Points:**
- **FROM IU CODE:** Source (IU or EXT_XXX for external)
- **TO IUGU CODE:** Destination (IU or GU)
- **TRANSPORT CODE:** T1 (direct/road), T2 (bulk/rail), etc.
- **TIME PERIOD:** 1, 2, or 3
- **FREIGHT COST:** Cost structure varies:
  - T1: ₹/ton (direct)
  - T2: ₹ per shipment regardless of quantity
- **HANDLING COST:** Additional fixed cost (often 0, sometimes 100-440 ₹)
- **QUANTITY MULTIPLIER:** 
  - T1 = 1 (single tons)
  - T2 = 3000 (full truck = 3000 tons)
  
**Total Transport Cost Formula:**
```
Total Transport Cost = (FREIGHT COST + HANDLING COST) × QUANTITY MULTIPLIER
```

**Data in File:**
- Routes: 318+ transportation records
- IU-to-IU routes (internal use, often 0 cost or minimal)
- IU-to-GU routes (main distribution)
- External sources (EXT_001, EXT_002) to specific GUs
- Missing routes are not allowed (route not in data = route not feasible)

**Excel Format Requirements for User Input:**
```
Column A: FROM IU CODE (validation: must exist in IUGUType)
Column B: TO IUGU CODE (validation: must exist in IUGUType)
Column C: TRANSPORT CODE (validation: T1, T2, T3, etc.)
Column D: TIME PERIOD (validation: 1-3)
Column E: FREIGHT COST (validation: >= 0, numeric)
Column F: HANDLING COST (validation: >= 0, numeric, optional = 0)
Column G: QUANTITY MULTIPLIER (validation: > 0, numeric, optional = 1)
```

---

### Sheet 5: IUGUConstraint
**Purpose:** Route-specific quantity limits & restrictions

**Structure:**
```
IU CODE | TRANSPORT CODE | IUGU CODE | TIME PERIOD | BOUND TYPEID | VALUE TYPEID | Value
IU_003  | T2             |           | 1           | L            | C            | 233200.0
IU_004  |                | GU_016    | 1           | E            | C            | 0.0
```

**Key Points:**
- **IU CODE:** Source plant
- **TRANSPORT CODE:** Specific mode (optional - if blank, constraint applies to all modes)
- **IUGU CODE:** Specific destination (optional - if blank, constraint applies to all destinations from source)
- **TIME PERIOD:** 1, 2, or 3
- **BOUND TYPEID:** 
  - **L** = Minimum (Lower bound)
  - **G** = Maximum (Greater bound) [CONFUSING NAMING, but used in data]
  - **E** = Exact (route not allowed)
- **VALUE TYPEID:** 
  - **C** = Quantity in tons
- **Value:** The constraint value

**Examples from Data:**
```
IU_003, T2, _, 1, L, C, 233200 → IU_003 via T2 must ship AT LEAST 233,200 tons total (all destinations combined)
IU_004, _, GU_016, 1, E, C, 0.0 → Route IU_004 to GU_016 is NOT ALLOWED (E = Exact 0)
```

**Data in File:**
- 80+ constraint records
- Most common: Minimum shipment quantities per mode from source
- Some: Route exclusions (E = 0)
- Some: Route minimums by destination (GU_023 minimum 44,118 tons from IU_015)

**Excel Format Requirements for User Input:**
```
Column A: IU CODE (validation: must be IU_XXX)
Column B: TRANSPORT CODE (optional)
Column C: IUGU CODE (optional)
Column D: TIME PERIOD (validation: 1-3)
Column E: BOUND TYPEID (validation: L, G, E)
Column F: VALUE TYPEID (validation: C)
Column G: Value (validation: numeric, >= 0)
```

---

### Sheet 6: IUGUOpeningStock
**Purpose:** Initial inventory at each plant at start of planning horizon

**Structure:**
```
IUGU CODE | OPENING STOCK
IU_002    | 14533.46
GU_001    | 2959.03
```

**Key Points:**
- **IUGU CODE:** Any plant (IU or GU)
- **OPENING STOCK:** Starting inventory in tons (decimal allowed)
- **Default Rule:** If not provided → Opening Stock = 0

**Data in File:**
- 46 plant records
- Range: 773.12 to 176,793.50 tons
- Accounts for existing stock before period 1

**Excel Format Requirements for User Input:**
```
Column A: IUGU CODE (validation: must exist in IUGUType)
Column B: OPENING STOCK (validation: >= 0, numeric, decimal allowed)
```

---

### Sheet 7: HubOpeningStock
**Purpose:** Central buffer/hub inventory (optional intermediate storage)

**Structure:**
```
IU  | IUGU | Opening Stock
IU_007 | GU_019 | 77343.30
```

**Key Points:**
- **IU:** Source Integrated Unit
- **IUGU:** Destination (usually GU, acts as hub/buffer)
- **Opening Stock:** Pre-positioned inventory
- **Default Rule:** If not given → No hub inventory

**Data in File:**
- 3 hub records
- Represents pre-positioned buffer stock for quick fulfillment

**Excel Format Requirements for User Input:**
```
Column A: IU (validation: IU_XXX)
Column B: IUGU (validation: GU_XXX)
Column C: Opening Stock (validation: >= 0, numeric)
```

---

### Sheet 8: IUGUClosingStock
**Purpose:** Safety stock & inventory bounds at end of each period

**Structure:**
```
IUGU CODE | TIME PERIOD | MIN CLOSE STOCK | MAX CLOSE STOCK
IU_002    | 1           | 14400.0         | 50000.0
GU_001    | 1           | 14100.0         | [blank]
```

**Key Points:**
- **IUGU CODE:** Any plant
- **TIME PERIOD:** 1, 2, or 3
- **MIN CLOSE STOCK:** Safety stock (must maintain at period end)
  - Default Rule: If not given → 0
- **MAX CLOSE STOCK:** Inventory capacity limit
  - Default Rule: If not given → Unlimited
- **Purpose:** Prevents stockouts (MIN) and overstocking (MAX)

**Data in File:**
- 135 records (45 plants × 3 periods)
- MIN ranges: 5,000 to 144,000 tons
- MAX ranges: 20,000 to 352,000 tons (for IUs), blank for most GUs
- MAX often blank for GUs (no maximum), strict for IUs (warehousing limits)

**Excel Format Requirements for User Input:**
```
Column A: IUGU CODE (validation: must exist in IUGUType)
Column B: TIME PERIOD (validation: 1-3)
Column C: MIN CLOSE STOCK (validation: >= 0, numeric, optional = 0)
Column D: MAX CLOSE STOCK (validation: >= 0, numeric, optional = unlimited)
```

---

### Sheet 9: IUGUType
**Purpose:** Plant classification & sourcing

**Structure:**
```
IUGU CODE | PLANT TYPE | # Source
GU_002    | GU         | [blank]
IU_020    | IU         | [blank]
GU_019    | GU         | 3.0
```

**Key Points:**
- **IUGU CODE:** Plant identifier
- **PLANT TYPE:** 
  - **IU** = Integrated Unit (produces clinker)
  - **GU** = Grinding Unit (consumes clinker)
- **# Source:** Number of supply sources (informational, used to validate feasibility)

**Data in File:**
- 45 plants total
- 20 IUs (producers)
- 25 GUs (consumers)

**Excel Format Requirements for User Input:**
```
Column A: IUGU CODE (validation: unique, alphanumeric)
Column B: PLANT TYPE (validation: IU or GU)
Column C: # Source (optional, numeric)
```

---

## 3. SYSTEM ARCHITECTURE & FLOW

### 3.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         USER INTERFACE (Frontend)                │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │ Manual Form      │  │ Excel Upload     │  │ API Gateway  │  │
│  │ (Single records) │  │ (Bulk data)      │  │              │  │
│  └────────┬─────────┘  └────────┬─────────┘  └──────┬───────┘  │
│           │                     │                    │           │
└───────────┼─────────────────────┼────────────────────┼───────────┘
            │                     │                    │
            └─────────────────────┼────────────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │    DATA VALIDATION &      │
                    │    PREPROCESSING          │
                    │    - Check mandatory      │
                    │    - Validate ranges      │
                    │    - Fill defaults        │
                    │    - Normalize formats    │
                    └─────────────┬──────────────┘
                                  │
            ┌─────────────────────▼──────────────────────┐
            │        DATABASE (PostgreSQL/MongoDB)       │
            │  ┌──────────────────────────────────────┐  │
            │  │ ClinkerDemand                        │  │
            │  │ ClinkerCapacity                      │  │
            │  │ ProductionCost                       │  │
            │  │ LogisticsIUGU                        │  │
            │  │ IUGUConstraint                       │  │
            │  │ IUGUOpeningStock                     │  │
            │  │ HubOpeningStock                      │  │
            │  │ IUGUClosingStock                     │  │
            │  │ IUGUType                             │  │
            │  └──────────────────────────────────────┘  │
            └─────────────────────┬──────────────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │  OPTIMIZATION ENGINE       │
                    │  (PuLP + Gurobi/CBC)      │
                    │  - Build LP/MIP model     │
                    │  - Load data from DB      │
                    │  - Run optimization       │
                    │  - Store results          │
                    └─────────────┬──────────────┘
                                  │
            ┌─────────────────────▼──────────────────────┐
            │      RESULTS DATABASE                      │
            │  ┌──────────────────────────────────────┐  │
            │  │ OptimalShipmentPlan                  │  │
            │  │ ProductionSchedule                   │  │
            │  │ InventoryProfile                     │  │
            │  │ CostBreakdown                        │  │
            │  └──────────────────────────────────────┘  │
            └─────────────────────┬──────────────────────┘
                                  │
            ┌─────────────────────▼──────────────────────┐
            │    REPORTING & ANALYTICS (Frontend)       │
            │  ┌──────────────────────────────────────┐  │
            │  │ Optimal Cost Report                  │  │
            │  │ Utilization Charts                   │  │
            │  │ Shipment Schedule                    │  │
            │  │ Inventory Tracking                   │  │
            │  └──────────────────────────────────────┘  │
            └─────────────────────────────────────────────┘
```

### 3.2 Data Flow - Input to Output

```
USER INPUT PHASE (Week 1-2 of hackathon)
├─ Option A: Manual Form Entry
│  ├─ User enters plant master data (IUGUType)
│  ├─ User enters demands (ClinkerDemand) by plant & period
│  ├─ User enters production capacities (ClinkerCapacity)
│  └─ System validates in real-time, shows errors
│
├─ Option B: Excel File Upload
│  ├─ User uploads Excel with all 9 sheets
│  ├─ System parses each sheet
│  ├─ Multi-sheet handling: automatic sheet detection & loading
│  ├─ Row-level validation: each row checked against rules
│  ├─ Missing data handling: applies defaults
│  ├─ Data transformation: formats to database schema
│  └─ Bulk insert to database (transaction)
│
└─ Option C: Hybrid
   ├─ Upload bulk data via Excel
   ├─ Manually override specific values
   └─ Re-validate complete dataset

PREPROCESSING PHASE
├─ Data Validation
│  ├─ Check mandatory fields (per sheet specification)
│  ├─ Validate ranges (DEMAND > 0, CAPACITY > 0, etc.)
│  ├─ Cross-reference validation (IU codes must exist in IUGUType)
│  ├─ Identify missing mandatory fields → reject record
│  └─ Log errors: file, sheet, row, error description
│
├─ Default Application
│  ├─ MIN_FULFILLMENT (%) blank → 100%
│  ├─ CAPACITY missing → 0
│  ├─ PRODUCTION_COST missing → 999,999 (penalty)
│  ├─ HANDLING_COST blank → 0
│  ├─ QUANTITY_MULTIPLIER blank → 1
│  ├─ OPENING_STOCK missing → 0
│  ├─ MIN_CLOSE_STOCK blank → 0
│  ├─ MAX_CLOSE_STOCK blank → ∞
│  └─ HubOpeningStock missing → no hub for that pair
│
├─ Data Normalization
│  ├─ Ensure all numeric values are floats/decimals
│  ├─ Plant codes: standardize format (IU_001, GU_001, etc.)
│  ├─ Time periods: map to integers (1, 2, 3)
│  └─ Costs: verify currency consistency
│
└─ Master Data Setup
   ├─ Build Plant Master from IUGUType
   ├─ Build Route Master from LogisticsIUGU
   ├─ Build Period Master (3 periods)
   └─ Cross-validate all references

OPTIMIZATION PHASE (Core Algorithm)
├─ Model Setup
│  ├─ Create decision variables: x_{ij t m} = quantity from i to j in period t via mode m
│  ├─ Create integer variable: trips_{ijt m} = number of trips
│  ├─ Load costs: production, transportation, inventory
│  └─ Load constraints: capacity, demand, stock, routes
│
├─ Constraint Building
│  ├─ Production constraint: total produced ≤ capacity
│  ├─ Demand constraint: total received ≥ demand × MIN_FULFILLMENT%
│  ├─ Inventory balance: prev_stock + produced + received - shipped = current_stock
│  ├─ Stock bounds: MIN_CLOSE_STOCK ≤ closing_stock ≤ MAX_CLOSE_STOCK
│  ├─ Route constraints: if missing route → quantity = 0
│  ├─ Minimum shipment: if shipped > 0 → shipped ≥ minimum batch
│  ├─ Trip integrity: quantity = trips × QUANTITY_MULTIPLIER
│  └─ Route exclusion: IU_X to GU_Y with E constraint → quantity = 0
│
├─ Objective Function
│  └─ Minimize: ∑ (Production_Cost × quantity_produced) 
│                + ∑ (Transport_Cost × quantity_shipped)
│                + ∑ (Holding_Cost × ending_stock)
│
├─ Solver Execution
│  ├─ Use CBC solver (free, decent) OR Gurobi (commercial, fast)
│  ├─ Set time limit: 5 minutes for MVP
│  ├─ Output: optimal solution or best feasible solution
│  └─ Store results: shipment plan, production schedule, inventory
│
└─ Post-Processing
   ├─ Extract solution values
   ├─ Calculate utilization rates
   ├─ Compute cost breakdown
   ├─ Generate compliance report
   └─ Store in Results DB

REPORTING PHASE
├─ Summary Report
│  ├─ Total minimum cost achieved: ₹X Crore
│  ├─ Cost breakdown: Production / Transportation / Inventory
│  ├─ Feasibility: All demands met? All constraints satisfied?
│  └─ Optimality gap (if solver stopped early)
│
├─ Operational Reports
│  ├─ Shipment Schedule (by period, from, to, mode, quantity, cost)
│  ├─ Production Schedule (by IU, by period, quantity, total cost)
│  ├─ Inventory Profile (by location, by period, opening/closing/peak)
│  ├─ Route Utilization (% of capacity used per route)
│  └─ Transport Mode Breakdown (% via T1 vs T2)
│
└─ Strategic Insights
   ├─ Capacity bottlenecks (plants operating at >95% capacity)
   ├─ Cost drivers (which routes/modes cost most)
   ├─ Inventory trends (if increasing/decreasing each period)
   └─ Recommendations for capacity expansion or mode optimization
```

---

## 4. IMPLEMENTATION ROADMAP (36-HOUR HACKATHON)

### Timeline Breakdown

```
HOUR 0-2: Planning & Setup
├─ Team alignment: data structure understanding
├─ Database schema design (SQL)
├─ API contract definition (endpoints)
└─ UI/UX mockups for input & output screens

HOUR 2-6: Backend Phase 1 - Data Layer
├─ Database setup (PostgreSQL with 9 tables)
├─ Data models & schema (Django/FastAPI ORM)
├─ API endpoints for CRUD operations
│  ├─ POST /api/data/upload-excel
│  ├─ POST /api/data/validate
│  ├─ GET /api/data/summary
│  └─ GET /api/data/details/{sheet_name}
├─ Input validation logic (server-side)
└─ Test with sample Excel file

HOUR 6-12: Backend Phase 2 - Optimization
├─ Install optimization libraries (PuLP + CBC)
├─ Build optimization model
│  ├─ Decision variables setup
│  ├─ Constraint generation logic
│  ├─ Objective function
│  └─ Solver configuration
├─ Create optimization job processor
├─ Store results in database
└─ Test with small dataset (5 plants, 1 period)

HOUR 12-18: Frontend + Integration
├─ Excel upload component (drag-drop)
├─ Form-based entry (if time permits)
├─ Manual form for critical inputs
├─ Real-time validation feedback
├─ Integrate with backend APIs
├─ Test end-to-end data flow
└─ Error handling & user guidance

HOUR 18-24: Testing & MVP Completion
├─ Full dataset test (45 plants, 3 periods, 3K records)
├─ Optimization runs & timing validation
├─ Results validation (are constraints satisfied?)
├─ Report generation
├─ UI polish & presentation
└─ Documentation

HOUR 24-36: Polish, Presentation & Bonus
├─ Final testing & bug fixes
├─ Performance optimization
├─ User documentation
├─ Presentation deck preparation
│  ├─ Problem statement
│  ├─ Solution architecture
│  ├─ Results (cost savings, utilization)
│  ├─ Technical implementation
│  └─ Future roadmap
├─ BONUS: Scenario analysis or demand uncertainty (if time)
└─ Final rehearsal & submission
```

---

## 5. MINIMUM VIABLE PRODUCT (MVP) REQUIREMENTS

### For SIH 2025 Appreciation:

**MUST HAVE (Non-negotiable):**

1. ✅ **Data Input System**
   - Accept Excel file with 9 sheets (or 8 core sheets)
   - Automatic multi-sheet parsing
   - Validation with error reporting
   - Store in database

2. ✅ **Optimization Engine**
   - Build LP/MIP model from data
   - Minimize total cost
   - Satisfy all demand constraints
   - Respect production & transport constraints
   - Solve to optimality (or feasible solution)

3. ✅ **Results Reporting**
   - Total minimum cost achieved
   - Optimal shipment plan (from, to, quantity, mode, cost)
   - Production schedule (which IU produces what in each period)
   - Constraint compliance check (all demands met? stock requirements satisfied?)

**NICE TO HAVE (If Time Permits):**

4. 🟡 Manual form input (bypass Excel for single records)
5. 🟡 Scenario comparison (what if demand increases 10%?)
6. 🟡 Visualization (charts of cost breakdown, utilization)
7. 🟡 What-if analysis (e.g., disable route X, re-optimize)

**BONUS CHALLENGE (If Exceptional Performance):**

8. 🟢 Demand uncertainty modeling (robust optimization)
9. 🟢 Stochastic demand scenarios

---

## 6. DATABASE SCHEMA DESIGN

### PostgreSQL Tables (9 core tables + metadata)

```sql
-- 1. Plant Master
CREATE TABLE plants (
    id SERIAL PRIMARY KEY,
    plant_code VARCHAR(10) UNIQUE NOT NULL,  -- IU_001, GU_001, etc.
    plant_type VARCHAR(5) NOT NULL,  -- IU or GU
    sources_count INT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 2. Demand
CREATE TABLE clinker_demand (
    id SERIAL PRIMARY KEY,
    plan_id INT,  -- links to planning scenario
    plant_code VARCHAR(10) NOT NULL,
    time_period INT NOT NULL,  -- 1, 2, 3
    demand_tons DECIMAL(12,2) NOT NULL,
    min_fulfillment_pct DECIMAL(5,2) DEFAULT 100,
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (plan_id) REFERENCES planning_scenarios(id),
    UNIQUE(plan_id, plant_code, time_period)
);

-- 3. Capacity
CREATE TABLE clinker_capacity (
    id SERIAL PRIMARY KEY,
    plan_id INT,
    plant_code VARCHAR(10) NOT NULL,  -- IU only
    time_period INT NOT NULL,
    capacity_tons DECIMAL(12,2) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (plan_id) REFERENCES planning_scenarios(id),
    UNIQUE(plan_id, plant_code, time_period)
);

-- 4. Production Cost
CREATE TABLE production_cost (
    id SERIAL PRIMARY KEY,
    plan_id INT,
    plant_code VARCHAR(10) NOT NULL,  -- IU only
    time_period INT NOT NULL,
    cost_per_ton DECIMAL(8,2) NOT NULL,  -- ₹/ton
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (plan_id) REFERENCES planning_scenarios(id),
    UNIQUE(plan_id, plant_code, time_period)
);

-- 5. Routes (Logistics)
CREATE TABLE routes (
    id SERIAL PRIMARY KEY,
    plan_id INT,
    from_code VARCHAR(10) NOT NULL,
    to_code VARCHAR(10) NOT NULL,
    transport_mode VARCHAR(10) NOT NULL,  -- T1, T2, etc.
    time_period INT NOT NULL,
    freight_cost DECIMAL(10,2) NOT NULL,
    handling_cost DECIMAL(10,2) DEFAULT 0,
    quantity_multiplier DECIMAL(8,2) DEFAULT 1,
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (plan_id) REFERENCES planning_scenarios(id),
    UNIQUE(plan_id, from_code, to_code, transport_mode, time_period)
);

-- 6. Route Constraints
CREATE TABLE route_constraints (
    id SERIAL PRIMARY KEY,
    plan_id INT,
    from_code VARCHAR(10) NOT NULL,
    transport_mode VARCHAR(10),  -- NULL means all modes
    to_code VARCHAR(10),  -- NULL means all destinations
    time_period INT NOT NULL,
    constraint_type VARCHAR(3),  -- L (lower), G (greater), E (exact/exclude)
    value_tons DECIMAL(12,2) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (plan_id) REFERENCES planning_scenarios(id)
);

-- 7. Opening Stock
CREATE TABLE opening_stock (
    id SERIAL PRIMARY KEY,
    plan_id INT,
    plant_code VARCHAR(10) NOT NULL,
    opening_stock_tons DECIMAL(12,2) DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (plan_id) REFERENCES planning_scenarios(id),
    UNIQUE(plan_id, plant_code)
);

-- 8. Hub Opening Stock
CREATE TABLE hub_opening_stock (
    id SERIAL PRIMARY KEY,
    plan_id INT,
    from_code VARCHAR(10) NOT NULL,  -- IU
    to_code VARCHAR(10) NOT NULL,  -- GU
    opening_stock_tons DECIMAL(12,2) DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (plan_id) REFERENCES planning_scenarios(id),
    UNIQUE(plan_id, from_code, to_code)
);

-- 9. Closing Stock Requirements
CREATE TABLE closing_stock_requirements (
    id SERIAL PRIMARY KEY,
    plan_id INT,
    plant_code VARCHAR(10) NOT NULL,
    time_period INT NOT NULL,
    min_close_stock_tons DECIMAL(12,2) DEFAULT 0,
    max_close_stock_tons DECIMAL(12,2),  -- NULL means unlimited
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (plan_id) REFERENCES planning_scenarios(id),
    UNIQUE(plan_id, plant_code, time_period)
);

-- Metadata Tables
CREATE TABLE planning_scenarios (
    id SERIAL PRIMARY KEY,
    scenario_name VARCHAR(100),
    description TEXT,
    data_upload_date TIMESTAMP DEFAULT NOW(),
    number_of_plants INT,
    number_of_periods INT,
    status VARCHAR(20),  -- "CREATED", "VALIDATING", "VALID", "OPTIMIZING", "COMPLETE"
    user_id INT
);

CREATE TABLE optimization_results (
    id SERIAL PRIMARY KEY,
    scenario_id INT,
    total_cost DECIMAL(16,2),
    production_cost DECIMAL(16,2),
    transportation_cost DECIMAL(16,2),
    holding_cost DECIMAL(16,2),
    solver_status VARCHAR(20),  -- "OPTIMAL", "FEASIBLE", "INFEASIBLE"
    optimality_gap DECIMAL(5,2),  -- percentage if not optimal
    solve_time_seconds INT,
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (scenario_id) REFERENCES planning_scenarios(id)
);

CREATE TABLE shipment_plan (
    id SERIAL PRIMARY KEY,
    result_id INT,
    from_code VARCHAR(10),
    to_code VARCHAR(10),
    transport_mode VARCHAR(10),
    time_period INT,
    quantity_tons DECIMAL(12,2),
    number_of_trips INT,
    transport_cost DECIMAL(12,2),
    FOREIGN KEY (result_id) REFERENCES optimization_results(id)
);

CREATE TABLE production_schedule (
    id SERIAL PRIMARY KEY,
    result_id INT,
    plant_code VARCHAR(10),
    time_period INT,
    quantity_produced_tons DECIMAL(12,2),
    production_cost DECIMAL(12,2),
    capacity_utilization_pct DECIMAL(5,2),
    FOREIGN KEY (result_id) REFERENCES optimization_results(id)
);

CREATE TABLE inventory_profile (
    id SERIAL PRIMARY KEY,
    result_id INT,
    plant_code VARCHAR(10),
    time_period INT,
    opening_stock_tons DECIMAL(12,2),
    production_tons DECIMAL(12,2),
    inbound_tons DECIMAL(12,2),
    outbound_tons DECIMAL(12,2),
    closing_stock_tons DECIMAL(12,2),
    FOREIGN KEY (result_id) REFERENCES optimization_results(id)
);
```

---

## 7. FRONTEND FORM SPECIFICATIONS

### Input Method 1: Excel Upload (PRIMARY)

**UI Component:**
```
┌─────────────────────────────────────────────────────────────┐
│  📤 UPLOAD CLINKER SUPPLY CHAIN DATA                        │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ Drop your Excel file here or click to select        │   │
│  │ (File must contain 8-9 sheets with standard format) │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  Expected Sheets:                                           │
│  ✅ ClinkerDemand                                          │
│  ✅ ClinkerCapacity                                        │
│  ✅ ProductionCost                                         │
│  ✅ LogisticsIUGU                                          │
│  ✅ IUGUConstraint      (Optional)                         │
│  ✅ IUGUOpeningStock                                       │
│  ✅ IUGUClosingStock                                       │
│  ✅ IUGUType                                               │
│  ✅ HubOpeningStock     (Optional)                         │
│                                                             │
│  [Choose File] | [Cancel] | [Upload & Validate]           │
└─────────────────────────────────────────────────────────────┘
```

**Processing Steps:**
1. User selects Excel file (XLSX)
2. Frontend sends to `/api/data/upload-excel`
3. Backend:
   - Reads all sheets
   - Detects sheet names automatically
   - Parses each row
   - Validates against schema
   - Reports errors with line numbers
4. Returns validation report:
   ```json
   {
     "status": "VALIDATION_ERRORS",
     "total_records": 2150,
     "processed": 2108,
     "errors": [
       {"sheet": "ClinkerDemand", "row": 45, "error": "DEMAND must be > 0"},
       {"sheet": "ProductionCost", "row": 12, "error": "PRODUCTION_COST missing"}
     ]
   }
   ```
5. User can:
   - Fix file & re-upload
   - Override errors (if permitted)
   - Proceed with valid records only

### Input Method 2: Manual Form (SECONDARY)

**Single Record Entry Form (if time permits):**

```
DEMAND ENTRY
┌──────────────────────────────────────┐
│ Plant Code: [IU_001 ▼]              │
│ Time Period: [1 ▼]                  │
│ Demand (tons): [51,565]             │
│ Min Fulfillment (%): [100]          │
│ [Add Record] [Save & Add More]      │
└──────────────────────────────────────┘

PRODUCTION COST ENTRY
┌──────────────────────────────────────┐
│ Plant Code: [IU_001 ▼]              │
│ Time Period: [1 ▼]                  │
│ Cost (₹/ton): [2119]                │
│ [Add Record] [Save & Add More]      │
└──────────────────────────────────────┘

ROUTE ENTRY
┌──────────────────────────────────────┐
│ From Plant: [IU_001 ▼]              │
│ To Plant: [GU_001 ▼]                │
│ Transport Mode: [T1 ▼]              │
│ Time Period: [1 ▼]                  │
│ Freight Cost (₹): [955.8]           │
│ Handling Cost (₹): [0]              │
│ Quantity Multiplier: [1]            │
│ [Add Route] [Save & Add More]       │
└──────────────────────────────────────┘
```

### Data Validation Rules (Frontend + Backend)

**Mandatory Fields by Sheet:**

```
ClinkerDemand:
  - IUGU CODE: mandatory, must exist in IUGUType
  - TIME PERIOD: mandatory, must be 1-3
  - DEMAND: mandatory, must be > 0
  - MIN_FULFILLMENT (%): optional, 0-100

ClinkerCapacity:
  - IU CODE: mandatory, must be IU_XXX
  - TIME PERIOD: mandatory, 1-3
  - CAPACITY: mandatory, must be >= 0

ProductionCost:
  - IU CODE: mandatory, must be IU_XXX
  - TIME PERIOD: mandatory, 1-3
  - PRODUCTION COST: mandatory, > 0

LogisticsIUGU:
  - FROM IU CODE: mandatory, must exist
  - TO IUGU CODE: mandatory, must exist
  - TRANSPORT CODE: mandatory, alphanumeric
  - TIME PERIOD: mandatory, 1-3
  - FREIGHT COST: mandatory, >= 0
  - HANDLING COST: optional, default 0
  - QUANTITY MULTIPLIER: optional, default 1

IUGUConstraint:
  - IU CODE: mandatory
  - BOUND TYPEID: mandatory, must be L/G/E
  - TIME PERIOD: mandatory, 1-3
  - Value: mandatory, numeric

IUGUOpeningStock:
  - IUGU CODE: mandatory, must exist
  - OPENING STOCK: mandatory, >= 0

IUGUClosingStock:
  - IUGU CODE: mandatory, must exist
  - TIME PERIOD: mandatory, 1-3
  - MIN_CLOSE_STOCK: optional, default 0
  - MAX_CLOSE_STOCK: optional, default unlimited

IUGUType:
  - IUGU CODE: mandatory, unique
  - PLANT TYPE: mandatory, IU or GU
  - # Source: optional
```

---

## 8. BACKEND PROCESSING PIPELINE

### 8.1 Data Ingestion & Validation

**Python Backend (FastAPI/Django):**

```python
# Step 1: Read Excel (using openpyxl or pandas)
import pandas as pd
from datetime import datetime

class ExcelDataProcessor:
    REQUIRED_SHEETS = {
        'ClinkerDemand': ['IUGU CODE', 'TIME PERIOD', 'DEMAND'],
        'ClinkerCapacity': ['IU CODE', 'TIME PERIOD', 'CAPACITY'],
        'ProductionCost': ['IU CODE', 'TIME PERIOD', 'PRODUCTION COST'],
        'LogisticsIUGU': ['FROM IU CODE', 'TO IUGU CODE', 'TRANSPORT CODE', 'TIME PERIOD', 'FREIGHT COST'],
        'IUGUOpeningStock': ['IUGU CODE', 'OPENING STOCK'],
        'IUGUClosingStock': ['IUGU CODE', 'TIME PERIOD', 'MIN CLOSE STOCK'],
        'IUGUType': ['IUGU CODE', 'PLANT TYPE'],
    }
    
    def read_excel(self, file_path):
        """Read all sheets from Excel"""
        excel_file = pd.ExcelFile(file_path)
        data = {}
        
        for sheet_name in excel_file.sheet_names:
            df = pd.read_sheet(file_path, sheet_name=sheet_name)
            data[sheet_name] = df
        
        return data
    
    def validate_data(self, data):
        """Validate each sheet"""
        errors = []
        
        for sheet_name, df in data.items():
            # Check required columns
            if sheet_name in self.REQUIRED_SHEETS:
                required_cols = self.REQUIRED_SHEETS[sheet_name]
                missing = set(required_cols) - set(df.columns)
                if missing:
                    errors.append({
                        'sheet': sheet_name,
                        'error': f'Missing columns: {missing}'
                    })
            
            # Validate rows
            for idx, row in df.iterrows():
                row_errors = self.validate_row(sheet_name, row)
                if row_errors:
                    errors.extend([{
                        'sheet': sheet_name,
                        'row': idx + 2,  # Excel line numbers
                        'error': err
                    } for err in row_errors])
        
        return errors
    
    def validate_row(self, sheet_name, row):
        """Validate individual row"""
        errors = []
        
        if sheet_name == 'ClinkerDemand':
            if pd.isna(row['IUGU CODE']):
                errors.append('IUGU CODE is mandatory')
            if pd.isna(row['DEMAND']) or row['DEMAND'] <= 0:
                errors.append('DEMAND must be > 0')
            if row['TIME PERIOD'] not in [1, 2, 3]:
                errors.append('TIME PERIOD must be 1, 2, or 3')
        
        elif sheet_name == 'ClinkerCapacity':
            if pd.isna(row['IU CODE']):
                errors.append('IU CODE is mandatory')
            if pd.isna(row['CAPACITY']):
                errors.append('CAPACITY is mandatory')
            if not row['IU CODE'].startswith('IU_'):
                errors.append('IU CODE must start with IU_')
        
        # ... similar validation for other sheets
        
        return errors
```

### 8.2 Default Values Application

```python
class DefaultsProcessor:
    def apply_defaults(self, data):
        """Apply default rules per sheet specification"""
        
        # ClinkerDemand
        if 'MIN FULFILLMENT (%)' in data['ClinkerDemand'].columns:
            data['ClinkerDemand']['MIN FULFILLMENT (%)'].fillna(100, inplace=True)
        
        # ProductionCost - missing cost gets high penalty
        for idx, row in data['ProductionCost'].iterrows():
            if pd.isna(row['PRODUCTION COST']):
                data['ProductionCost'].at[idx, 'PRODUCTION COST'] = 999999
        
        # LogisticsIUGU defaults
        data['LogisticsIUGU']['HANDLING COST'].fillna(0, inplace=True)
        data['LogisticsIUGU']['QUANTITY MULTIPLIER'].fillna(1, inplace=True)
        
        # IUGUOpeningStock
        data['IUGUOpeningStock']['OPENING STOCK'].fillna(0, inplace=True)
        
        # IUGUClosingStock
        data['IUGUClosingStock']['MIN CLOSE STOCK'].fillna(0, inplace=True)
        data['IUGUClosingStock']['MAX CLOSE STOCK'].fillna(np.inf, inplace=True)
        
        return data
```

### 8.3 Optimization Model Builder

```python
from pulp import *
import pandas as pd

class ClinkerOptimizationModel:
    def __init__(self, scenario_id):
        self.scenario_id = scenario_id
        self.model = LpProblem("ClinkerAllocation", LpMinimize)
        self.load_data()
        self.build_model()
    
    def load_data(self):
        """Load all data from database"""
        self.plants = pd.read_sql(
            "SELECT * FROM plants WHERE scenario_id = %s",
            params=[self.scenario_id]
        )
        self.demand = pd.read_sql(
            "SELECT * FROM clinker_demand WHERE plan_id = %s",
            params=[self.scenario_id]
        )
        self.capacity = pd.read_sql(
            "SELECT * FROM clinker_capacity WHERE plan_id = %s",
            params=[self.scenario_id]
        )
        # ... load other data
    
    def build_model(self):
        """Build optimization model"""
        
        # Decision Variables
        # x[i,j,t,m] = quantity shipped from i to j in period t via mode m
        self.x = {}
        for _, row in self.routes.iterrows():
            key = (row['from_code'], row['to_code'], row['time_period'], row['transport_mode'])
            self.x[key] = LpVariable(f"x_{key}", lowBound=0, cat='Continuous')
        
        # trips[i,j,t,m] = number of trips from i to j in period t via mode m
        self.trips = {}
        for key in self.x.keys():
            self.trips[key] = LpVariable(f"trips_{key}", lowBound=0, cat='Integer')
        
        # Production variables: prod[i,t] = quantity produced at IU i in period t
        self.prod = {}
        for _, row in self.plants.iterrows():
            if row['plant_type'] == 'IU':
                for t in [1, 2, 3]:
                    key = (row['plant_code'], t)
                    self.prod[key] = LpVariable(f"prod_{key}", lowBound=0, cat='Continuous')
        
        # Inventory: stock[i,t] = closing stock at plant i at end of period t
        self.stock = {}
        for _, row in self.plants.iterrows():
            for t in [1, 2, 3]:
                key = (row['plant_code'], t)
                self.stock[key] = LpVariable(f"stock_{key}", lowBound=0, cat='Continuous')
        
        # Objective: Minimize total cost
        self.model += (
            lpSum([
                self.prod[key] * self.cost_prod[key] 
                for key in self.prod.keys()
            ]) +
            lpSum([
                self.x[key] * self.cost_transport[key] 
                for key in self.x.keys()
            ]) +
            lpSum([
                self.stock[key] * 5  # ₹5 per ton per period holding cost
                for key in self.stock.keys()
            ])
        ), "TotalCost"
        
        # Constraints: Production Capacity
        for iu_code in self.plants[self.plants['plant_type'] == 'IU']['plant_code']:
            for t in [1, 2, 3]:
                cap = self.get_capacity(iu_code, t)
                if cap:
                    self.model += (
                        self.prod[(iu_code, t)] <= cap,
                        f"capacity_{iu_code}_{t}"
                    )
        
        # Constraints: Demand Fulfillment
        for _, demand_row in self.demand.iterrows():
            plant = demand_row['plant_code']
            t = demand_row['time_period']
            dem = demand_row['demand_tons']
            min_fulfill = demand_row['min_fulfillment_pct'] / 100
            
            # Inbound quantity = sum of all shipments received
            inbound = lpSum([
                self.x[(from_code, plant, t, mode)]
                for (from_code, to_code, to_t, mode) in self.x.keys()
                if to_code == plant and to_t == t
            ])
            
            self.model += (
                inbound >= dem * min_fulfill,
                f"demand_{plant}_{t}"
            )
        
        # Constraints: Inventory Balance
        for plant_code in self.plants['plant_code']:
            for t in [1, 2, 3]:
                # Opening stock
                opening = self.get_opening_stock(plant_code, t)
                
                # Production (if IU)
                prod_qty = self.prod.get((plant_code, t), 0)
                
                # Inbound shipments
                inbound = lpSum([
                    self.x[(from_c, plant_code, t, mode)]
                    for (from_c, to_c, t_period, mode) in self.x.keys()
                    if to_c == plant_code and t_period == t
                ])
                
                # Outbound shipments
                outbound = lpSum([
                    self.x[(plant_code, to_c, t, mode)]
                    for (from_c, to_c, t_period, mode) in self.x.keys()
                    if from_c == plant_code and t_period == t
                ])
                
                # Closing stock constraint
                self.model += (
                    opening + prod_qty + inbound - outbound == self.stock[(plant_code, t)],
                    f"inventory_balance_{plant_code}_{t}"
                )
        
        # Constraints: Closing Stock Bounds (Safety Stock & Capacity)
        for _, stock_req in self.closing_stock.iterrows():
            plant = stock_req['plant_code']
            t = stock_req['time_period']
            min_stock = stock_req['min_close_stock_tons']
            max_stock = stock_req['max_close_stock_tons']
            
            self.model += (
                self.stock[(plant, t)] >= min_stock,
                f"min_stock_{plant}_{t}"
            )
            
            if not pd.isna(max_stock):
                self.model += (
                    self.stock[(plant, t)] <= max_stock,
                    f"max_stock_{plant}_{t}"
                )
        
        # Constraints: Trip Quantity Relationship
        for key, x_var in self.x.items():
            from_c, to_c, t, mode = key
            qty_mult = self.get_quantity_multiplier(from_c, to_c, t, mode)
            
            self.model += (
                x_var == self.trips[key] * qty_mult,
                f"trip_qty_{key}"
            )
        
        # Constraints: Route Exclusions & Minimums
        for _, constraint in self.constraints.iterrows():
            from_c = constraint['from_code']
            to_c = constraint['to_code']
            mode = constraint['transport_mode']
            bound_type = constraint['constraint_type']
            value = constraint['value_tons']
            t = constraint['time_period']
            
            if bound_type == 'E' and value == 0:  # Route excluded
                self.model += (
                    lpSum([
                        self.x[(from_c, to_c, t, m)]
                        for (f, to, t_p, m) in self.x.keys()
                        if f == from_c and to == to_c and t_p == t
                    ]) == 0,
                    f"exclude_route_{from_c}_{to_c}_{t}"
                )
            
            elif bound_type == 'L':  # Minimum shipment
                self.model += (
                    lpSum([
                        self.x[(from_c, to_c, t, m)]
                        for (f, to, t_p, m) in self.x.keys()
                        if f == from_c and t_p == t and (to == to_c if to_c else True)
                    ]) >= value,
                    f"min_shipment_{from_c}_{t}"
                )
    
    def solve(self):
        """Solve the model"""
        self.model.solve(PULP_CBC_CMD(timeLimit=300, msg=0))
        
        return {
            'status': LpStatus[self.model.status],
            'objective_value': value(self.model.objective),
            'solution': self.extract_solution()
        }
    
    def extract_solution(self):
        """Extract solution to friendly format"""
        shipments = []
        for key, var in self.x.items():
            if var.varValue and var.varValue > 0:
                from_c, to_c, t, mode = key
                qty = var.varValue
                trips = self.trips[key].varValue
                
                shipments.append({
                    'from': from_c,
                    'to': to_c,
                    'transport_mode': mode,
                    'period': t,
                    'quantity_tons': qty,
                    'number_of_trips': int(trips),
                    'transport_cost': qty * self.cost_transport[key]
                })
        
        return shipments
```

---

## 9. OPTIMIZATION MODEL OVERVIEW

### 9.1 Mathematical Formulation

**Decision Variables:**
```
x_{ijt}^m = Quantity (tons) shipped from plant i to plant j in period t via mode m
t_{ijt}^m = Number of trips from i to j in period t via mode m (integer)
p_{it} = Quantity (tons) produced at IU i in period t
s_{it} = Closing stock (inventory) at plant i at end of period t
```

**Objective Function:**
```
Minimize Z = ∑∑∑∑ (freight_cost_{ijt}^m + handling_cost_{ijt}^m) × x_{ijt}^m
           + ∑∑ production_cost_{it} × p_{it}
           + ∑∑ holding_cost_{it} × s_{it}

Where:
- holding_cost_{it} = ₹5/ton/period (implicit inventory carrying cost)
```

**Subject to Constraints:**

**1. Production Capacity:**
```
p_{it} ≤ capacity_{it}  ∀ IU i, period t
```

**2. Demand Fulfillment (with partial fulfillment allowed):**
```
∑_m ∑_i x_{ijt}^m ≥ demand_{jt} × min_fulfillment%_{jt}  ∀ plant j, period t
```

**3. Inventory Balance:**
```
opening_stock_{it} + p_{it} + ∑_m ∑_k x_{kit}^m - ∑_m ∑_j x_{ijt}^m = s_{it}

∀ plant i, period t

Explanation:
  opening_stock_{it} = stock at beginning of period t at plant i
  p_{it} = production at IU i in period t (0 for GU)
  ∑ x_{kit}^m = total inbound shipments to plant i
  ∑ x_{ijt}^m = total outbound shipments from plant i
  s_{it} = closing stock at end of period t at plant i
```

**4. Closing Stock Bounds (Safety Stock & Storage Capacity):**
```
min_close_stock_{it} ≤ s_{it} ≤ max_close_stock_{it}  ∀ plant i, period t
```

**5. Trip Quantity Relationship:**
```
x_{ijt}^m = t_{ijt}^m × quantity_multiplier_{ijt}^m  ∀ i,j,t,m

Example:
  T1 mode: 1 trip = 1 ton
  T2 mode: 1 trip = 3000 tons
```

**6. Route Exclusions:**
```
If constraint_type = E (exclude) with value = 0:
  x_{ijt}^m = 0  (route not allowed)

If constraint_type = E (exact):
  x_{ijt}^m = exact_value
```

**7. Minimum Shipment Quantities (per mode or route):**
```
If constraint_type = L (lower bound):
  ∑_m ∑_j (x_{ijt}^m | from=i, period=t) ≥ minimum_qty

Ensures economic shipment batch sizes
```

**8. Non-negativity:**
```
x_{ijt}^m ≥ 0  ∀ i,j,t,m
p_{it} ≥ 0  ∀ i,t
s_{it} ≥ 0  ∀ i,t
t_{ijt}^m ≥ 0 and integer  ∀ i,j,t,m
```

### 9.2 Model Complexity

**Approximate Model Size (with 45 plants, 3 periods, 20+ routes per plant):**

```
Decision Variables:
  x variables: ~1,800 (continuous)
  t variables: ~1,800 (integer) ← Makes it MIP (harder to solve)
  p variables: 60 (production)
  s variables: 135 (inventory)
  
Total: ~3,795 variables (1,800 integer)

Constraints:
  Production capacity: 60
  Demand: 210
  Inventory balance: 135
  Stock bounds: 270
  Trip quantity: 1,800
  Route constraints: 80+
  
Total: ~2,550 constraints

This is a MEDIUM-SCALE MIP problem
- CBC solver: 5-30 minutes to optimality
- Gurobi solver: 10 seconds to optimality
- For hackathon: set time limit to 5 minutes, get good feasible solution
```

---

## 10. DELIVERABLES CHECKLIST FOR HACKATHON

### Minimum Viable Product (MVP) - Must Have

```
☐ Data Input System
  ☐ Excel upload with multi-sheet parsing
  ☐ Automatic sheet detection
  ☐ Row-level validation
  ☐ Error reporting with line numbers
  ☐ Database storage
  
☐ Optimization Engine
  ☐ LP/MIP model built correctly
  ☐ All constraints implemented
  ☐ Objective function minimizing cost
  ☐ Solver integration (CBC or Gurobi)
  ☐ Handles 3-month planning horizon
  
☐ Results & Reporting
  ☐ Total minimum cost calculated
  ☐ Cost breakdown (production, transport, holding)
  ☐ Optimal shipment plan (from, to, mode, qty, cost)
  ☐ Production schedule by IU & period
  ☐ Constraint compliance verification
  ☐ Feasibility check (all demands met?)
```

### Presentation Package (Must Have for Appreciation)

```
☐ Problem Statement Document
  ☐ Clear problem description
  ☐ Industry context
  ☐ Complexity explanation
  ☐ Business impact
  
☐ Solution Architecture
  ☐ System diagram (data→model→results)
  ☐ Database schema
  ☐ API specification
  ☐ Optimization model (mathematical formulation)
  
☐ Implementation Demo
  ☐ Live demo: upload Excel → see results
  ☐ Show sample data loading
  ☐ Show optimization running
  ☐ Display results dashboard
  
☐ Results & Insights
  ☐ Cost optimization achieved (₹ savings or %)
  ☐ Utilization rates (production, transport)
  ☐ Key findings (bottlenecks, efficient routes)
  ☐ Comparison: with vs without optimization
  
☐ Technical Documentation
  ☐ Code structure & key classes
  ☐ Data flow explanation
  ☐ Optimization model description
  ☐ Known limitations
  
☐ Future Roadmap
  ☐ Phase 2 enhancements
  ☐ Demand uncertainty modeling
  ☐ What-if scenarios
  ☐ Scalability to full supply chain
```

### Code & Repo Structure

```
clinker-optimization/
├── frontend/
│   ├── pages/
│   │   ├── upload.tsx
│   │   ├── optimization.tsx
│   │   └── results.tsx
│   ├── components/
│   │   ├── FileUpload.tsx
│   │   ├── ValidationReport.tsx
│   │   └── ResultsDashboard.tsx
│   └── api/
│       └── client.ts
│
├── backend/
│   ├── api/
│   │   ├── data_endpoints.py
│   │   ├── optimization_endpoints.py
│   │   └── results_endpoints.py
│   │
│   ├── services/
│   │   ├── data_processor.py
│   │   ├── optimization_engine.py
│   │   └── report_generator.py
│   │
│   ├── models/
│   │   ├── database.py
│   │   └── schemas.py
│   │
│   └── main.py (FastAPI app)
│
├── database/
│   ├── schema.sql
│   └── migrations/
│
├── data/
│   ├── sample_data.xlsx
│   └── validation_rules.json
│
├── tests/
│   ├── test_data_processor.py
│   ├── test_optimization.py
│   └── test_api.py
│
├── docs/
│   ├── PROBLEM_STATEMENT.md
│   ├── SOLUTION_DESIGN.md
│   ├── USER_GUIDE.md
│   └── TECHNICAL_SPEC.md
│
└── README.md
```

---

## 11. CRITICAL IMPLEMENTATION NOTES

### What NOT to Do (Common Mistakes)

1. ❌ **Don't hardcode plant data** - Load from database
2. ❌ **Don't ignore Excel structure** - Parse sheets generically
3. ❌ **Don't apply penalties for missing data** - Use defaults from spec
4. ❌ **Don't forget integer constraints on trips** - Makes solver job harder but necessary
5. ❌ **Don't compute holding cost manually** - Let solver optimize it
6. ❌ **Don't ignore route exclusions** - Check constraint type = E
7. ❌ **Don't skip validation** - Users will give bad data

### Performance Tips for 36-Hour Timeline

1. ✅ **Use sample data first** - Test with 5 plants, 1 period before full dataset
2. ✅ **Set solver time limit** - 5 minutes max to avoid hanging
3. ✅ **Parallelize if possible** - Run multiple scenarios in background
4. ✅ **Cache database queries** - Avoid repeated reads
5. ✅ **Pre-compute costs** - Calculate transport cost = (freight + handling) × multiplier once
6. ✅ **Use incremental development** - Get basic model working, then add constraints
7. ✅ **Test edge cases** - Zero demand, zero capacity, excluded routes

### Success Metrics for Evaluation

```
Judges will assess:

Technical Correctness (40%)
  □ Optimization model correctly formulated
  □ All constraints properly implemented
  □ Solution is mathematically valid

Business Impact (30%)
  □ Cost reduction achieved
  □ Realistic & practical results
  □ Clear business value demonstrated

Code Quality (20%)
  □ Clean, readable code
  □ Proper error handling
  □ Database design appropriate

Presentation (10%)
  □ Clear explanation of problem
  □ Live demo working
  □ Professional documentation
```

---

## SUMMARY FOR YOUR TEAM

### For Milan (Lead):
- Owns system architecture, database design, integration
- Ensures all 9 sheets are correctly loaded
- Validates data completeness & defaults

### For Rahul (Backend):
- Builds optimization engine using PuLP
- Implements all 8 constraint categories
- Solves model, extracts results, stores in DB

### For Jay (Frontend):
- Creates Excel upload UI
- Implements validation feedback
- Builds results dashboard & reporting

### Deliverable Priority:
1. **Hour 0-6:** Database + API + Excel parsing
2. **Hour 6-18:** Optimization model + solver
3. **Hour 18-24:** Results dashboard + polish
4. **Hour 24-36:** Documentation + presentation

---

**Generated:** January 8, 2026 | SIH 2025 Preparation