import os
import sys
import json
from datetime import datetime

# Add backend to path
sys.path.append(os.path.join(os.getcwd()))

from database import SessionLocal, AISignal, Trade
import intelligence_feedback

def test_attribution():
    db = SessionLocal()
    try:
        print("Running attribution analysis test...")
        intelligence_feedback.run_attribution_analysis()
        
        report_path = "intelligence_attribution_report.json"
        if os.path.exists(report_path):
            with open(report_path, "r") as f:
                report = json.load(f)
            print(f"Success! Report generated. Keys: {list(report.keys())}")
            print(f"Summary: {report.get('summary', 'No summary')}")
        else:
            print("Failed: Report file not found.")
            
    finally:
        db.close()

if __name__ == "__main__":
    test_attribution()
