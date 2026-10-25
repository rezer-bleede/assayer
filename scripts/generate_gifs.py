import PIL.Image as Image
import PIL.ImageDraw as ImageDraw
import PIL.ImageFont as ImageFont
import imageio
import os

width, height = 760, 440

def get_font(size=14, bold=False):
    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        return ImageFont.truetype(font_path, size)
    except Exception:
        return ImageFont.load_default()

font_title = get_font(18, True)
font_sub = get_font(12, False)
font_body = get_font(13, False)
font_bold = get_font(13, True)
font_code = get_font(12, False)

def draw_header(draw, title_text):
    draw.rectangle([0, 0, width, 44], fill='#0f172a')
    draw.ellipse([16, 16, 26, 26], fill='#ef4444')
    draw.ellipse([34, 16, 44, 26], fill='#f59e0b')
    draw.ellipse([52, 16, 62, 26], fill='#10b981')

    draw.text((80, 14), f"Assayer — {title_text}", fill='#f8fafc', font=font_bold)
    draw.rectangle([width - 130, 10, width - 16, 34], fill='#1e293b', outline='#334155')
    draw.text((width - 120, 14), "● Agent Active", fill='#38bdf8', font=font_sub)

def draw_card(draw, x, y, w, h, bg='#1e293b', border='#334155'):
    draw.rectangle([x, y, x + w, y + h], fill=bg, outline=border, width=1)

# Usecase 1: ETL Lineage & Transformation Parsing
def make_gif1():
    frames = []

    steps = [
        ("Ingesting SQL & Python AST", "Parsing raw.orders -> analytics.daily_revenue", [
            ("SELECT order_id, customer_id, amount FROM raw.orders", "#38bdf8"),
            ("Lineage target: analytics.daily_revenue", "#94a3b8"),
            ("Status: Analyzing AST & CTEs...", "#f59e0b")
        ]),
        ("Lineage Graph Extracted", "1 Table Lineage Node, 2 Upstream Sources Found", [
            ("analytics.daily_revenue <= raw.orders JOIN raw.customers", "#10b981"),
            ("Risk Flag: Non-idempotent append on target table", "#ef4444"),
            ("Risk Flag: INNER JOIN may drop unmatched orders", "#f59e0b")
        ]),
        ("Transformation Vulnerability Scan", "3 Quality Risk Flags Identified", [
            ("[CRITICAL] Unmatched customer_id orphan key risk", "#ef4444"),
            ("[WARNING]  Null value propagation in revenue sum", "#f59e0b"),
            ("[INFO]     Schema Contract: 5 columns mapped perfectly", "#10b981")
        ])
    ]

    for title, sub, items in steps:
        img = Image.new('RGB', (width, height), color='#090d16')
        draw = ImageDraw.Draw(img)
        draw_header(draw, "1. ETL Lineage & Transformation Analysis")

        draw_card(draw, 20, 60, 350, 360, bg='#0f172a')
        draw.text((36, 76), "Input ETL Pipeline (SQL / Python)", fill='#f8fafc', font=font_bold)

        code_lines = [
            "-- models/daily_revenue.sql",
            "WITH filtered_orders AS (",
            "  SELECT order_id, customer_id, amount",
            "  FROM raw.orders WHERE status = 'COMPLETED'",
            ")",
            "INSERT INTO analytics.daily_revenue",
            "SELECT c.id, SUM(o.amount)",
            "FROM filtered_orders o",
            "JOIN raw.customers c ON o.customer_id = c.id",
            "GROUP BY 1;"
        ]
        cy = 110
        for line in code_lines:
            draw.text((36, cy), line, fill='#94a3b8', font=font_code)
            cy += 24

        draw_card(draw, 390, 60, 350, 360, bg='#1e293b')
        draw.text((406, 76), title, fill='#38bdf8', font=font_bold)
        draw.text((406, 98), sub, fill='#94a3b8', font=font_sub)

        iy = 140
        for item_text, item_color in items:
            draw_card(draw, 406, iy, 318, 64, bg='#0f172a', border=item_color)
            draw.text((418, iy + 20), item_text, fill=item_color, font=font_body)
            iy += 80

        for _ in range(15):
            frames.append(img)

    imageio.mimsave('docs/assets/usecase1-etl-lineage.gif', frames, fps=10, loop=0)

# Usecase 2: Data Validation & Drift Detection
def make_gif2():
    frames = []
    steps = [
        ("Profiling Live Database & Seed Files", "Sampling raw.orders & postgresql://wh:5432/dwh", [
            ("Inspecting column types & row counts...", "#38bdf8"),
            ("Analyzing value distribution for 'amount'", "#94a3b8"),
            ("Comparing seed raw_customers.csv with live schema", "#94a3b8")
        ]),
        ("Schema Drift & Distribution Anomaly Detected", "1 Contract Drift, 1 Out-of-bounds Value", [
            ("Column 'phone_number' missing in live warehouse", "#ef4444"),
            ("Value Drift: Negative amount (-$45.00) in 1 row", "#f59e0b"),
            ("Row count: 12,450 live vs 12,000 baseline", "#10b981")
        ]),
        ("Drift Audit Summary Ready", "100% Non-destructive profiling completed", [
            ("Null Rate: 0.02% (Pass < 1.0%)", "#10b981"),
            ("Uniqueness: raw.orders.order_id 1 duplicate found", "#ef4444"),
            ("Freshness SLA: 18h lag (Warning > 12h)", "#f59e0b")
        ])
    ]

    for title, sub, items in steps:
        img = Image.new('RGB', (width, height), color='#090d16')
        draw = ImageDraw.Draw(img)
        draw_header(draw, "2. Data Validation & Drift Detection")

        draw_card(draw, 20, 60, 350, 360, bg='#0f172a')
        draw.text((36, 76), "Live Data Sources Profiled", fill='#f8fafc', font=font_bold)

        sources = [
            ("Table: raw.orders (12,450 rows)", "#38bdf8"),
            ("  • order_id: INT (PK, 99.99% Unique)", "#e2e8f0"),
            ("  • amount: DECIMAL (Min: -$45, Max: $1200)", "#f59e0b"),
            ("  • created_at: TIMESTAMP (Freshness: 18h)", "#f59e0b"),
            ("Seed: raw_customers.csv (1,200 rows)", "#38bdf8"),
            ("  • customer_id: INT (PK)", "#e2e8f0"),
            ("  • email: VARCHAR (1 invalid pattern)", "#f59e0b"),
            ("  • phone_number: Missing in Target DB!", "#ef4444")
        ]
        cy = 110
        for line, col in sources:
            draw.text((36, cy), line, fill=col, font=font_code)
            cy += 24

        draw_card(draw, 390, 60, 350, 360, bg='#1e293b')
        draw.text((406, 76), title, fill='#38bdf8', font=font_bold)
        draw.text((406, 98), sub, fill='#94a3b8', font=font_sub)

        iy = 140
        for item_text, item_color in items:
            draw_card(draw, 406, iy, 318, 64, bg='#0f172a', border=item_color)
            draw.text((418, iy + 20), item_text, fill=item_color, font=font_body)
            iy += 80

        for _ in range(15):
            frames.append(img)

    imageio.mimsave('docs/assets/usecase2-data-profiling.gif', frames, fps=10, loop=0)

# Usecase 3: Test Planning Across Targets
def make_gif3():
    frames = []
    steps = [
        ("Synthesizing Baseline Checks", "LLM-augmented rule engine analyzing target", [
            ("Drafting primary key uniqueness checks...", "#38bdf8"),
            ("Drafting foreign key referential integrity checks...", "#94a3b8"),
            ("Drafting web route availability & UI assertions...", "#94a3b8")
        ]),
        ("Interactive Plan Review", "Human-in-the-loop review before execution", [
            ("29 checks proposed across 4 categories", "#10b981"),
            ("Target: Postgres DWH & Next.js Web App", "#38bdf8"),
            ("Status: Awaiting Human Approval [Approve Button]", "#f59e0b")
        ]),
        ("Plan Approved by Engineer", "Execution triggered across read-only probes", [
            ("Approved: 29 checks queued for execution", "#10b981"),
            ("Compiled aggregate SELECT queries ready", "#10b981"),
            ("Running non-destructive audit suite...", "#38bdf8")
        ])
    ]

    for title, sub, items in steps:
        img = Image.new('RGB', (width, height), color='#090d16')
        draw = ImageDraw.Draw(img)
        draw_header(draw, "3. Test Planning Across Targets")

        draw_card(draw, 20, 60, 350, 360, bg='#0f172a')
        draw.text((36, 76), "Proposed Test Suite (29 Checks)", fill='#f8fafc', font=font_bold)

        plan_items = [
            ("✔ Check 1: raw.orders.order_id Uniqueness", "#10b981"),
            ("✔ Check 2: daily_revenue FK -> raw.customers", "#10b981"),
            ("✔ Check 3: Freshness SLA < 24h", "#10b981"),
            ("✔ Check 4: Non-negative amount constraint", "#10b981"),
            ("✔ Check 5: Web UI Route /dashboard load", "#10b981"),
            ("✔ Check 6: Web UI Parity vs DWH Query", "#10b981"),
            ("✔ Check 7: Visual screenshot drift check", "#10b981"),
            ("... and 22 additional targeted probes", "#94a3b8")
        ]
        cy = 110
        for line, col in plan_items:
            draw.text((36, cy), line, fill=col, font=font_code)
            cy += 28

        draw_card(draw, 390, 60, 350, 360, bg='#1e293b')
        draw.text((406, 76), title, fill='#38bdf8', font=font_bold)
        draw.text((406, 98), sub, fill='#94a3b8', font=font_sub)

        iy = 140
        for item_text, item_color in items:
            draw_card(draw, 406, iy, 318, 64, bg='#0f172a', border=item_color)
            draw.text((418, iy + 20), item_text, fill=item_color, font=font_body)
            iy += 80

        for _ in range(15):
            frames.append(img)

    imageio.mimsave('docs/assets/usecase3-test-planning.gif', frames, fps=10, loop=0)

# Usecase 4: QA Results & Scorecard Reporting
def make_gif4():
    frames = []
    steps = [
        ("Executing Read-Only Probes", "29 Checks Executed in Parallel (4 workers)", [
            ("Executing SQL probes against wh database...", "#38bdf8"),
            ("Capturing sample defect rows...", "#94a3b8"),
            ("Calculating overall quality score...", "#94a3b8")
        ]),
        ("Quality Scorecard Generated", "Overall Quality Score: 85.31 / 100", [
            ("Critical Defect: 1 orphan customer row found", "#ef4444"),
            ("Critical Defect: 1 duplicate order_id found", "#ef4444"),
            ("Warning: 1 invalid email format detected", "#f59e0b")
        ]),
        ("AI Root-Cause Analysis Ready", "Diagnostic Narrative & Fix Recommendation", [
            ("Root Cause: Missing foreign key constraint on target", "#38bdf8"),
            ("Sample Row Captured: customer_id = 99999", "#e2e8f0"),
            ("Export Available: JSON / CSV audit report", "#10b981")
        ])
    ]

    for title, sub, items in steps:
        img = Image.new('RGB', (width, height), color='#090d16')
        draw = ImageDraw.Draw(img)
        draw_header(draw, "4. QA Results & Scorecard Reporting")

        draw_card(draw, 20, 60, 350, 360, bg='#0f172a')
        draw.text((36, 76), "Audit Results & Scorecard", fill='#f8fafc', font=font_bold)

        draw_card(draw, 36, 110, 318, 80, bg='#1e1b4b', border='#6366f1')
        draw.text((50, 125), "QA SCORE: 85.31 / 100", fill='#818cf8', font=font_title)
        draw.text((50, 155), "Grade: B+ (2 Critical Defects Found)", fill='#cbd5e1', font=font_sub)

        res_items = [
            ("CRITICAL: daily_revenue orphan customer_id (1 row)", "#ef4444"),
            ("CRITICAL: raw.orders duplicate order_id (1 row)", "#ef4444"),
            ("WARNING: raw.customers invalid email format", "#f59e0b"),
            ("INFO: raw.orders negative amount (-$45)", "#38bdf8"),
            ("PASS: 25 checks passed with zero errors", "#10b981")
        ]
        cy = 210
        for line, col in res_items:
            draw.text((36, cy), line, fill=col, font=font_code)
            cy += 28

        draw_card(draw, 390, 60, 350, 360, bg='#1e293b')
        draw.text((406, 76), title, fill='#38bdf8', font=font_bold)
        draw.text((406, 98), sub, fill='#94a3b8', font=font_sub)

        iy = 140
        for item_text, item_color in items:
            draw_card(draw, 406, iy, 318, 64, bg='#0f172a', border=item_color)
            draw.text((418, iy + 20), item_text, fill=item_color, font=font_body)
            iy += 80

        for _ in range(15):
            frames.append(img)

    imageio.mimsave('docs/assets/usecase4-audit-reporting.gif', frames, fps=10, loop=0)

make_gif1()
make_gif2()
make_gif3()
make_gif4()
print("All 4 preview gifs generated successfully!")
