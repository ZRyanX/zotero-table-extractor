#!/usr/bin/env python3
"""
tests/test_mineru_popo.py — MinerU-Popo 可选后处理钩子与跨平台硬件自动检测单元测试。
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
for p in (ROOT_DIR, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts import common, mineru_popo, table_postprocess


class TestMinerUPopoHardwareAndActivation(unittest.TestCase):
    def setUp(self):
        mineru_popo._HW_CACHE = None

    def tearDown(self):
        mineru_popo._HW_CACHE = None

    def test_detect_hardware_cuda_apple_silicon_and_cpu(self):
        # 1. NVIDIA CUDA (Windows / Linux)
        with patch.object(mineru_popo.platform, "system", return_value="Windows"), \
             patch.object(mineru_popo.platform, "machine", return_value="AMD64"), \
             patch.object(mineru_popo, "_probe_apple_silicon_chip", return_value=(False, "")), \
             patch.object(mineru_popo, "_probe_nvidia_smi", return_value=(True, "NVIDIA GeForce RTX 4090", 24.0)):
            hw = mineru_popo.detect_popo_hardware(force_refresh=True)
            self.assertTrue(hw["eligible"])
            self.assertTrue(hw["cuda_available"])
            self.assertFalse(hw["apple_silicon"])
            self.assertEqual(hw["accelerator"], "cuda")
            self.assertEqual(hw["device"], "cuda")
            self.assertEqual(hw["device_name"], "NVIDIA GeForce RTX 4090")
            self.assertEqual(hw["vram_gb"], 24.0)

        # 2. Apple Silicon M-series (macOS arm64)
        with patch.object(mineru_popo.platform, "system", return_value="Darwin"), \
             patch.object(mineru_popo.platform, "machine", return_value="arm64"), \
             patch.object(mineru_popo, "_probe_apple_silicon_chip", return_value=(True, "Apple M3 Max")), \
             patch.object(mineru_popo, "_probe_nvidia_smi", return_value=(False, "", None)):
            hw_m = mineru_popo.detect_popo_hardware(force_refresh=True)
            self.assertTrue(hw_m["eligible"])
            self.assertTrue(hw_m["apple_silicon"])
            self.assertTrue(hw_m["mps_available"])
            self.assertEqual(hw_m["accelerator"], "apple_silicon")
            self.assertEqual(hw_m["device"], "mps")
            self.assertEqual(hw_m["device_name"], "Apple M3 Max")

        # 3. CPU-only host
        with patch.object(mineru_popo.platform, "system", return_value="Linux"), \
             patch.object(mineru_popo.platform, "machine", return_value="x86_64"), \
             patch.object(mineru_popo, "_probe_apple_silicon_chip", return_value=(False, "")), \
             patch.object(mineru_popo, "_probe_nvidia_smi", return_value=(False, "", None)):
            hw_cpu = mineru_popo.detect_popo_hardware(force_refresh=True)
            self.assertFalse(hw_cpu["eligible"])
            self.assertEqual(hw_cpu["accelerator"], "cpu")
            self.assertEqual(hw_cpu["device"], "cpu")

    def test_three_state_activation_default_off_and_hardware_auto(self):
        hw_cuda = {"eligible": True, "accelerator": "cuda", "device": "cuda"}
        hw_apple = {"eligible": True, "accelerator": "apple_silicon", "device": "mps"}
        hw_cpu = {"eligible": False, "accelerator": "cpu", "device": "cpu"}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINERU_POPO_ENABLED", None)
            os.environ.pop("POPO_VLLM_URL", None)
            os.environ.pop("MINERU_POPO_VLLM_URL", None)

            # Default OFF even on CUDA / Apple Silicon
            en, reason = mineru_popo.should_enable_mineru_popo(
                config={"MINERU_POPO_ENABLED": False}, hw_info=hw_cuda
            )
            self.assertFalse(en)
            self.assertEqual(reason, "disabled_by_default")

            # auto mode enables on CUDA & Apple Silicon M-series, stays off on CPU
            en_cuda, r_cuda = mineru_popo.should_enable_mineru_popo(
                config={"MINERU_POPO_ENABLED": "auto"}, hw_info=hw_cuda
            )
            self.assertTrue(en_cuda)
            self.assertEqual(r_cuda, "auto_hardware:cuda")

            en_apple, r_apple = mineru_popo.should_enable_mineru_popo(
                config={"MINERU_POPO_ENABLED": "auto"}, hw_info=hw_apple
            )
            self.assertTrue(en_apple)
            self.assertEqual(r_apple, "auto_hardware:apple_silicon")

            en_cpu, r_cpu = mineru_popo.should_enable_mineru_popo(
                config={"MINERU_POPO_ENABLED": "auto"}, hw_info=hw_cpu
            )
            self.assertFalse(en_cpu)
            self.assertEqual(r_cpu, "auto_hardware_ineligible_cpu")

            # auto mode with remote vLLM URL works even on CPU client
            en_vllm, r_vllm = mineru_popo.should_enable_mineru_popo(
                config={"MINERU_POPO_ENABLED": "auto", "MINERU_POPO_VLLM_URL": "http://127.0.0.1:8000/v1"},
                hw_info=hw_cpu,
            )
            self.assertTrue(en_vllm)
            self.assertEqual(r_vllm, "auto_remote_vllm")

            # explicit ON on CPU respects MINERU_POPO_AUTO_HARDWARE guard unless explicitly disabled
            en_blk, r_blk = mineru_popo.should_enable_mineru_popo(
                config={"MINERU_POPO_ENABLED": True, "MINERU_POPO_AUTO_HARDWARE": True},
                hw_info=hw_cpu,
            )
            self.assertFalse(en_blk)
            self.assertEqual(r_blk, "explicit_on_blocked_cpu_only")

            en_force, r_force = mineru_popo.should_enable_mineru_popo(
                config={"MINERU_POPO_ENABLED": True, "MINERU_POPO_AUTO_HARDWARE": False},
                hw_info=hw_cpu,
            )
            self.assertTrue(en_force)
            self.assertEqual(r_force, "explicit_on_cpu_forced")

    def test_common_load_config_supports_official_popo_env_aliases(self):
        with patch.dict(
            os.environ,
            {
                "MINERU_POPO_ENABLED": "auto",
                "POPO_INFERENCE_BACKEND": "vllm",
                "POPO_VLLM_URL": "http://127.0.0.1:8123/v1",
                "POPO_MODEL_NAME": "MinerU-Popo-4B",
                "POPO_MODEL_PATH": "/models/mineru-popo",
            },
        ):
            cfg = common.load_config()
            self.assertEqual(cfg.get("MINERU_POPO_ENABLED"), "auto")
            self.assertEqual(cfg.get("MINERU_POPO_BACKEND"), "vllm")
            self.assertEqual(cfg.get("MINERU_POPO_VLLM_URL"), "http://127.0.0.1:8123/v1")
            self.assertEqual(cfg.get("MINERU_POPO_MODEL_NAME"), "MinerU-Popo-4B")
            self.assertEqual(cfg.get("MINERU_POPO_MODEL_PATH"), "/models/mineru-popo")


class TestMinerUPopoInferenceAndHook(unittest.TestCase):
    def test_vllm_client_and_postprocess_hook_merges_and_preserves_safety_gates(self):
        df1 = pd.DataFrame({
            "Sample": ["ZK-01", "ZK-02"],
            "Lithology": ["Granite", "Diorite (altered by"],
            "Au_gt": ["1.2", "3.4"],
        })
        df1.attrs = {"label": "Table 1", "page_idx": 4, "footnotes": ["a: Fire assay."]}

        # Intentionally distinct-looking column names so rule-based merge alone keeps them separate
        df2 = pd.DataFrame({
            "Code": ["", "ZK-03"],
            "RockDesc": ["hydrothermal fluids)", "Basalt"],
            "Grade": ["", "0.9"],
        })
        df2.attrs = {"page_idx": 5}

        mock_resp = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({
                                "is_continuation": 1,
                                "cell_merge_vector": [0, 1, 0],
                                "recovered_caption": "Table 1. Assay results",
                                "recovered_footnotes": ["b.d.l. = below detection limit."],
                                "confidence": 0.94,
                            })
                        }
                    }
                ]
            },
        )
        cfg = {
            "MINERU_POPO_ENABLED": "auto",
            "MINERU_POPO_VLLM_URL": "http://127.0.0.1:8000",
            "MINERU_POPO_BACKEND": "vllm",
        }
        hw_cuda = {"eligible": True, "accelerator": "cuda", "device": "cuda"}

        with patch.object(mineru_popo.requests, "post", return_value=mock_resp) as mock_post:
            merged = mineru_popo.run_popo_postprocess_hook([df1, df2], config=cfg, hw_info=hw_cuda)
            mock_post.assert_called_once()
            self.assertEqual(len(merged), 1)
            res = merged[0]
            self.assertEqual(len(res), 3)
            self.assertEqual(res.iloc[1]["Lithology"], "Diorite (altered by hydrothermal fluids)")
            self.assertEqual(res.attrs.get("covered_pages"), [4, 5])
            self.assertTrue(res.attrs.get("popo_postprocessed"))
            self.assertIn("a: Fire assay.", res.attrs.get("footnotes", []))
            self.assertIn("b.d.l. = below detection limit.", res.attrs.get("footnotes", []))

    def test_popo_hook_vetoes_conflicting_table_numbers_and_numeric_cell_collisions(self):
        # 1. Explicit conflicting table numbers never call the model or merge
        t1 = pd.DataFrame({"Sample": ["S1"], "Val": ["1.0"]})
        t1.attrs = {"label": "Table 1", "page_idx": 1}
        t2 = pd.DataFrame({"Sample": ["S2"], "Val": ["2.0"]})
        t2.attrs = {"label": "Table 2", "page_idx": 2}
        fake_client = MagicMock()
        fake_client.resolve_active_backend.return_value = "vllm"
        out = mineru_popo.run_popo_postprocess_hook(
            [t1, t2],
            config={"MINERU_POPO_ENABLED": "auto"},
            client=fake_client,
            hw_info={"eligible": True, "accelerator": "apple_silicon", "device": "mps"},
        )
        self.assertEqual(len(out), 2)
        fake_client.analyze_table_pair.assert_not_called()

        # 2. Numeric-to-numeric boundary conflict vetoes cell collapse even if model says q_j=1
        p = pd.DataFrame({"Sample": ["S1"], "SiO2": ["70.1"]})
        n = pd.DataFrame({"Sample": [""], "SiO2": ["71.5"]})
        p_out, n_out = mineru_popo._apply_verified_cell_merge_vector(p, n, [0, 1])
        self.assertEqual(len(p_out), 1)
        self.assertEqual(len(n_out), 1)
        self.assertEqual(p_out.iloc[0]["SiO2"], "70.1")
        self.assertEqual(n_out.iloc[0]["SiO2"], "71.5")

    def test_merge_continuation_tables_invokes_popo_hook_only_when_enabled(self):
        df1 = pd.DataFrame({"Sample": ["S1", "S2"], "SiO2": ["70.1", "71.2"]})
        df1.attrs = {"label": "Table 1", "page_idx": 1}
        df2 = pd.DataFrame({"Code": ["S3", "S4"], "Oxide": ["72.0", "69.5"]})
        df2.attrs = {"page_idx": 2}

        # Default disabled -> hook not invoked
        with patch.dict(os.environ, {"MINERU_POPO_ENABLED": "false"}), \
             patch.object(mineru_popo, "run_popo_postprocess_hook") as mock_hook:
            res_off = table_postprocess.merge_continuation_tables([df1, df2])
            self.assertEqual(len(res_off), 2)
            mock_hook.assert_not_called()

        # Enabled (auto + CUDA) -> hook invoked
        with patch.dict(os.environ, {"MINERU_POPO_ENABLED": "auto"}), \
             patch.object(mineru_popo, "detect_popo_hardware", return_value={"eligible": True, "accelerator": "cuda", "device": "cuda"}), \
             patch.object(mineru_popo, "run_popo_postprocess_hook", return_value=[df1]) as mock_hook:
            res_on = table_postprocess.merge_continuation_tables([df1, df2])
            self.assertEqual(len(res_on), 1)
            mock_hook.assert_called_once()

    def test_setup_env_reports_mineru_popo_and_optional_with_popo_install(self):
        import setup_env

        report = setup_env.check_readiness()
        self.assertIn("mineru_popo", report)
        self.assertIn("hardware_eligible", report["mineru_popo"])
        self.assertIn("accelerator", report["mineru_popo"])

        res_default = setup_env.install_python_deps(with_all=True, with_popo=False, dry_run=True)
        self.assertFalse(any(p.startswith("transformers") for p in res_default["installed"]))

        res_popo = setup_env.install_python_deps(with_popo=True, dry_run=True)
        self.assertTrue(any(p.startswith("transformers") for p in res_popo["installed"]))


if __name__ == "__main__":
    unittest.main()
