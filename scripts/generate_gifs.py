#!/usr/bin/env python3
import os
from PIL import Image, ImageDraw, ImageFont

# Ensure target directory exists
os.makedirs("docs/assets", exist_ok=True)

# Font loading with fallback
def get_fonts():
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 20)
        subtitle_font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 15)
        body_font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", 13)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", 11)
    except Exception:
        title_font = subtitle_font = body_font = small_font = ImageFont.load_default()
    return title_font, subtitle_font, body_font, small_font

title_font, subtitle_font, body_font, small_font = get_fonts()

# Color Palette (Dark Theme)
BG_COLOR = (15, 23, 42)        # Slate 900
PANEL_BG = (30, 41, 59)        # Slate 800
BORDER_COLOR = (51, 65, 85)     # Slate 700
TEXT_MAIN = (241, 245, 249)     # Slate 100
TEXT_MUTED = (148, 163, 184)    # Slate 400
ACCENT_BLUE = (56, 189, 248)    # Sky 400
ACCENT_GREEN = (74, 222, 128)   # Emerald 400
ACCENT_AMBER = (251, 191, 36)   # Amber 400
ACCENT_RED = (248, 113, 113)    # Red 400
ACCENT_PURPLE = (192, 132, 252) # Purple 400

WIDTH, HEIGHT = 640, 380

def create_window_frame(title, category_badge):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Top window bar
    draw.rectangle([0, 0, WIDTH, 36], fill=(30, 41, 59))
    draw.line([0, 36, WIDTH, 36], fill=BORDER_COLOR, width=1)

    # Window control dots
    draw.ellipse([14, 12, 24, 22], fill=(239, 68, 68))
    draw.ellipse([32, 12, 42, 22], fill=(245, 158, 11))
    draw.ellipse([50, 12, 60, 22], fill=(34, 197, 94))

    # Title text
    draw.text((75, 10), title, font=subtitle_font, fill=TEXT_MAIN)

    # Category badge
    badge_w = 110
    draw.rectangle([WIDTH - badge_w - 15, 8, WIDTH - 15, 28], fill=(51, 65, 85))
    draw.text((WIDTH - badge_w - 8, 11), category_badge, font=small_font, fill=ACCENT_BLUE)

    # Main canvas panel
    draw.rectangle([15, 48, WIDTH - 15, HEIGHT - 15], fill=PANEL_BG, outline=BORDER_COLOR, width=1)

    return img, draw

# ==========================================
# GIF 1: ETL & Lineage Analysis
# ==========================================
def generate_usecase1_gif():
    frames = []

    steps = [
        ("Step 1: Ingesting ETL Pipeline Code", [
            ("SQL", "INSERT INTO analytics.daily_revenue", ACCENT_BLUE),
            ("SQL", "SELECT o.order_id, c.customer_id, o.amount", TEXT_MUTED),
            ("SQL", "FROM raw.orders o INNER JOIN raw.customers c", TEXT_MUTED),
            ("SQL", "ON o.customer_id = c.customer_id", TEXT_MUTED),
            ("STATUS", "[Parsing via sqlglot & AST...]", ACCENT_AMBER)
        ]),
        ("Step 2: Constructing Lineage Map", [
            ("TABLE", "raw.orders (id, amount, created_at)", ACCENT_PURPLE),
            ("TABLE", "raw.customers (id, email, status)", ACCENT_PURPLE),
            ("FLOW", "    └────► JOIN ──► analytics.daily_revenue", ACCENT_BLUE),
            ("LINEAGE", "Column Lineage: 3 sources -> 1 target model", TEXT_MAIN),
            ("STATUS", "[Lineage graph successfully built]", ACCENT_GREEN)
        ]),
        ("Step 3: Risk Flags & Transformation Audit", [
            ("RISK", "⚠ Inner Join: Risk of row loss on missing customer_id", ACCENT_AMBER),
            ("RISK", "⚠ Unfiltered SELECT: Potential unhandled NULL amounts", ACCENT_AMBER),
            ("RISK", "✓ Idempotency: Target schema uses strict table keys", ACCENT_GREEN),
            ("AUDIT", "Lineage analysis complete: 2 risk factors flagged", ACCENT_BLUE),
            ("STATUS", "[Ready for test plan generation]", ACCENT_GREEN)
        ])
    ]

    for step_title, lines in steps:
        for pulse in range(3):
            img, draw = create_window_frame("Qualitest · Use Case 1", "ETL LINEAGE")

            # Step header
            draw.rectangle([25, 58, WIDTH - 25, 88], fill=(15, 23, 42))
            draw.text((35, 64), step_title, font=subtitle_font, fill=ACCENT_BLUE)

            # Content box
            y = 100
            for label, text, color in lines:
                draw.rectangle([35, y, 110, y + 22], fill=(51, 65, 85))
                draw.text((42, y + 3), label, font=small_font, fill=ACCENT_BLUE)
                draw.text((120, y + 3), text, font=body_font, fill=color)
                y += 32

            # Footer progress bar
            draw.rectangle([35, HEIGHT - 35, WIDTH - 35, HEIGHT - 25], fill=(51, 65, 85))
            prog_w = int((steps.index((step_title, lines)) + 1) / len(steps) * (WIDTH - 70))
            draw.rectangle([35, HEIGHT - 35, 35 + prog_w, HEIGHT - 25], fill=ACCENT_BLUE)

            frames.append(img)

    frames[0].save(
        "docs/assets/usecase1-etl-lineage.gif",
        save_all=True,
        append_images=frames[1:],
        duration=1200,
        loop=0
    )
    print("Generated usecase1-etl-lineage.gif")

# ==========================================
# GIF 2: Data Profiling & Schema Drift
# ==========================================
def generate_usecase2_gif():
    frames = []

    steps = [
        ("Step 1: Connecting Data Sources & Seeds", [
            ("SOURCE", "PostgreSQL: analytics_db (wh.internal:5432)", ACCENT_BLUE),
            ("SEED", "CSV: seeds/raw_customers.csv (40 MB, 12 cols)", TEXT_MUTED),
            ("DUCKDB", "Parquet/CSV profiled in-memory via DuckDB", ACCENT_PURPLE),
            ("STATUS", "[Connecting and sampling sources...]", ACCENT_AMBER)
        ]),
        ("Step 2: Deep Column & Distribution Profiling", [
            ("PROFILE", "raw.orders.order_id   : PK · 10,000 rows · 0% null", ACCENT_GREEN),
            ("PROFILE", "raw.orders.amount     : Float · Range [0.00, 499.00]", TEXT_MAIN),
            ("PROFILE", "raw.customers.email   : Varchar · 0.01% invalid format", ACCENT_AMBER),
            ("STATUS", "[Profiled 3 tables, 28 columns in 45ms]", ACCENT_GREEN)
        ]),
        ("Step 3: Schema Contract & Drift Verification", [
            ("CONTRACT", "Seed raw_customers.csv  ──► Live Database Table", ACCENT_BLUE),
            ("DRIFT", "✖ Column missing in DW: 'signup_channel'", ACCENT_RED),
            ("DRIFT", "✔ Type match: 'created_at' -> TIMESTAMP", ACCENT_GREEN),
            ("STATUS", "ALERT: Schema contract drift detected in 1 column!", ACCENT_RED)
        ])
    ]

    for step_title, lines in steps:
        for pulse in range(3):
            img, draw = create_window_frame("Qualitest · Use Case 2", "DATA PROFILING")

            draw.rectangle([25, 58, WIDTH - 25, 88], fill=(15, 23, 42))
            draw.text((35, 64), step_title, font=subtitle_font, fill=ACCENT_AMBER)

            y = 100
            for label, text, color in lines:
                draw.rectangle([35, y, 120, y + 22], fill=(51, 65, 85))
                draw.text((42, y + 3), label, font=small_font, fill=ACCENT_AMBER)
                draw.text((130, y + 3), text, font=body_font, fill=color)
                y += 32

            draw.rectangle([35, HEIGHT - 35, WIDTH - 35, HEIGHT - 25], fill=(51, 65, 85))
            prog_w = int((steps.index((step_title, lines)) + 1) / len(steps) * (WIDTH - 70))
            draw.rectangle([35, HEIGHT - 35, 35 + prog_w, HEIGHT - 25], fill=ACCENT_AMBER)

            frames.append(img)

    frames[0].save(
        "docs/assets/usecase2-data-profiling.gif",
        save_all=True,
        append_images=frames[1:],
        duration=1200,
        loop=0
    )
    print("Generated usecase2-data-profiling.gif")

# ==========================================
# GIF 3: Automated Test Plan Generation
# ==========================================
def generate_usecase3_gif():
    frames = []

    steps = [
        ("Step 1: Synthesizing Baseline & LLM Test Rules", [
            ("ENGINE", "Deterministic Planner: Key integrity, NULL checks", ACCENT_BLUE),
            ("LLM", "LLM Plan Builder: Business logic & SLA thresholds", ACCENT_PURPLE),
            ("RULE", "Deriving null thresholds from observed rates", TEXT_MUTED),
            ("STATUS", "[Drafting 29 automated test probes...]", ACCENT_AMBER)
        ]),
        ("Step 2: Draft Plan Review (Human-in-the-Loop)", [
            ("PLAN", "Critical Probes : 8 (Foreign Keys, Uniqueness, Freshness)", ACCENT_RED),
            ("PLAN", "Warning Probes  : 15 (Email patterns, Range checks)", ACCENT_AMBER),
            ("PLAN", "Info Probes     : 6 (Row count parity, Non-negative)", ACCENT_GREEN),
            ("STATUS", "[Awaiting user approval: 29 checks staged]", ACCENT_BLUE)
        ]),
        ("Step 3: Human Approval & Execution Dispatch", [
            ("ACTION", "User Action: Clicked [ Approve & Run Plan ]", ACCENT_GREEN),
            ("SAFETY", "Re-validated: All probes compiled to read-only SELECTs", ACCENT_GREEN),
            ("WORKER", "Dispatched to Redis queue (Concurrency: 4 workers)", ACCENT_BLUE),
            ("STATUS", "STATUS: RUNNING (29 checks executing)", ACCENT_GREEN)
        ])
    ]

    for step_title, lines in steps:
        for pulse in range(3):
            img, draw = create_window_frame("Qualitest · Use Case 3", "TEST PLANNING")

            draw.rectangle([25, 58, WIDTH - 25, 88], fill=(15, 23, 42))
            draw.text((35, 64), step_title, font=subtitle_font, fill=ACCENT_PURPLE)

            y = 100
            for label, text, color in lines:
                draw.rectangle([35, y, 115, y + 22], fill=(51, 65, 85))
                draw.text((42, y + 3), label, font=small_font, fill=ACCENT_PURPLE)
                draw.text((125, y + 3), text, font=body_font, fill=color)
                y += 32

            draw.rectangle([35, HEIGHT - 35, WIDTH - 35, HEIGHT - 25], fill=(51, 65, 85))
            prog_w = int((steps.index((step_title, lines)) + 1) / len(steps) * (WIDTH - 70))
            draw.rectangle([35, HEIGHT - 35, 35 + prog_w, HEIGHT - 25], fill=ACCENT_PURPLE)

            frames.append(img)

    frames[0].save(
        "docs/assets/usecase3-test-planning.gif",
        save_all=True,
        append_images=frames[1:],
        duration=1200,
        loop=0
    )
    print("Generated usecase3-test-planning.gif")

# ==========================================
# GIF 4: Audit Execution & Scorecard
# ==========================================
def generate_usecase4_gif():
    frames = []

    steps = [
        ("Step 1: Running Read-Only Probes", [
            ("PROBE", "SELECT customer_id FROM analytics.daily_revenue...", TEXT_MUTED),
            ("PROBE", "SELECT order_id, COUNT(*) FROM raw.orders...", TEXT_MUTED),
            ("SAFETY", "assert_readonly() verified: 0 write side-effects", ACCENT_GREEN),
            ("STATUS", "[29/29 probes executed in 1.2s]", ACCENT_BLUE)
        ]),
        ("Step 2: Quality Scorecard & Defects Found", [
            ("SCORE", "DATA QUALITY SCORE: 85.31 / 100", ACCENT_GREEN),
            ("FAIL", "CRITICAL: Foreign Key Orphan (1 customer_id missing)", ACCENT_RED),
            ("FAIL", "CRITICAL: Duplicate order_id in raw.orders (1 dup)", ACCENT_RED),
            ("WARN", "WARNING : Freshness SLA breached (~28h vs 24h SLA)", ACCENT_AMBER)
        ]),
        ("Step 3: Failure Samples & Root Cause Narrative", [
            ("SAMPLE", "Captured failing row: customer_id = 'CST_9981'", ACCENT_RED),
            ("AI CAUSE", "'Orphan customer_id stems from INNER JOIN in", ACCENT_PURPLE),
            ("AI CAUSE", " daily_revenue.sql missing fallback handling.'", ACCENT_PURPLE),
            ("STATUS", "[Audit Report & CSV Export Ready]", ACCENT_GREEN)
        ])
    ]

    for step_title, lines in steps:
        for pulse in range(3):
            img, draw = create_window_frame("Qualitest · Use Case 4", "AUDIT REPORTING")

            draw.rectangle([25, 58, WIDTH - 25, 88], fill=(15, 23, 42))
            draw.text((35, 64), step_title, font=subtitle_font, fill=ACCENT_GREEN)

            y = 100
            for label, text, color in lines:
                draw.rectangle([35, y, 115, y + 22], fill=(51, 65, 85))
                draw.text((42, y + 3), label, font=small_font, fill=ACCENT_GREEN)
                draw.text((125, y + 3), text, font=body_font, fill=color)
                y += 32

            draw.rectangle([35, HEIGHT - 35, WIDTH - 35, HEIGHT - 25], fill=(51, 65, 85))
            prog_w = int((steps.index((step_title, lines)) + 1) / len(steps) * (WIDTH - 70))
            draw.rectangle([35, HEIGHT - 35, 35 + prog_w, HEIGHT - 25], fill=ACCENT_GREEN)

            frames.append(img)

    frames[0].save(
        "docs/assets/usecase4-audit-reporting.gif",
        save_all=True,
        append_images=frames[1:],
        duration=1200,
        loop=0
    )
    print("Generated usecase4-audit-reporting.gif")

if __name__ == "__main__":
    generate_usecase1_gif()
    generate_usecase2_gif()
    generate_usecase3_gif()
    generate_usecase4_gif()
    print("All 4 use case GIFs generated successfully.")
