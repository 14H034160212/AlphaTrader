import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import blog_monitor as bm


class BlogMonitorPriorityTests(unittest.TestCase):
    def test_priority_rules_detect_model_release_layoff_and_nvidia_partnership(self):
        model_impacts = bm._match_impact(
            "OpenAI releases GPT-5 for enterprise customers",
            "The new model launch expands AI infrastructure demand"
        )
        self.assertTrue(any(i.get("event_type") == "model_release" for i in model_impacts))

        layoff_impacts = bm._match_impact(
            "Meta announces 5,000 layoffs and restructuring",
            "Workforce reduction is aimed at improving margins"
        )
        self.assertTrue(any(i.get("event_type") == "layoff" for i in layoff_impacts))

        partnership_impacts = bm._match_impact(
            "NVIDIA partners with Oracle to deploy Blackwell GPUs",
            "The collaboration expands sovereign AI capacity"
        )
        self.assertTrue(any(i.get("event_type") == "nvidia_partnership" for i in partnership_impacts))


if __name__ == "__main__":
    unittest.main()
