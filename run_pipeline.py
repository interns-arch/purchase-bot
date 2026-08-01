import subprocess
import sys

scripts = [
    "inventory_import.py",
    "order_matching.py",
]

# Paused after order_matching.py (2026-07-31): the workflow was redesigned so
# order_matching.py only searches vendor inventory and writes a Vendor
# Comparison Report -- it no longer picks a vendor per order line.
# po_generator.py (and everything below it) needs a *chosen* vendor per line
# to create a real Purchase Order, and that Vendor Selection module (manual
# or rule-based) hasn't been built yet. Uncomment these once it exists:
#
# scripts += [
#     "po_generator.py",
#     "delivery_import.py",
#     "gap_analysis.py",
#     "alternative_vendor.py",
#     "vendor_performance.py",
#     "summary_report.py",
# ]

for script in scripts:
    print(f"\n=== Running {script} ===")
    result = subprocess.run([sys.executable, script])
    if result.returncode != 0:
        print(f"Error while running {script}")
        break

print("\nPipeline completed.")
print(
    "\nNote: pipeline currently stops after order_matching.py -- "
    "Vendor Selection is a future module (see run_pipeline.py comments)."
)