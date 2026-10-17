# -*- coding: utf-8 -*-
"""配置加载守卫（DD-3.1 阈值配置化；IT-2.02 禁硬编码；DD-16 R-07）。

原则：**缺键/非法即致命**（ConfigError），禁止静默兜底——参照
scripts/ci/pricing_baseline.json 单源教训（评审 N3）。
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unittest

import src.nlu.config as config_mod
from src.nlu.config import Config, ConfigError, load_config

NLU_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mutate(mutator):
    """取默认配置副本并按 mutator 破坏，返回修改后的 dict。"""
    with open(config_mod.DEFAULTS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    data = copy.deepcopy(data)
    mutator(data)
    return data


class TestDefaults(unittest.TestCase):
    def test_load_ok(self):
        cfg = load_config(env={})
        self.assertEqual(os.path.basename(config_mod.DEFAULTS_PATH),
                         "nlu.defaults.json")
        self.assertTrue(cfg.source)

    def test_conf_low_default_is_0_6(self):
        cfg = load_config(env={})
        # 值取自配置单源（阈值 nlu.conf_low，DD-3.1 默认 0.6）
        self.assertEqual(cfg.threshold("conf_low"), 0.6)
        self.assertEqual(cfg.conf_low, cfg.threshold("conf_low"))

    def test_threshold_order_is_consistent(self):
        cfg = load_config(env={})
        self.assertLess(cfg.conf_oos, cfg.conf_low)
        self.assertLessEqual(cfg.conf_low, cfg.conf_rule_weak)
        self.assertLessEqual(cfg.conf_rule_weak, cfg.conf_rule_strong)

    def test_vocab_covers_all_seven_intents(self):
        from src.nlu.intents import INTENTS
        cfg = load_config(env={})
        for intent in INTENTS:
            v = cfg.vocab(intent)
            for bucket in ("strong", "weak", "regex_strong", "regex_weak"):
                self.assertIsInstance(v[bucket], list, f"{intent}.{bucket}")
        self.assertEqual(set(cfg.priority()), set(INTENTS))


class TestMissingKeyFatal(unittest.TestCase):
    """缺键必须致命，不得回退默认或静默跳过。"""

    def setUp(self):
        self._orig = config_mod.DEFAULTS_PATH

    def tearDown(self):
        config_mod.DEFAULTS_PATH = self._orig

    def _load(self, data, env=None):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        config_mod.DEFAULTS_PATH = path
        return load_config(env=env if env is not None else {})

    def test_missing_threshold_key(self):
        data = _mutate(lambda d: d["thresholds"].pop("conf_low"))
        with self.assertRaises(ConfigError) as ctx:
            self._load(data)
        self.assertIn("conf_low", str(ctx.exception))

    def test_missing_template_key(self):
        data = _mutate(lambda d: d["templates"].pop("out_of_scope_guide"))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_missing_vocab_bucket(self):
        data = _mutate(lambda d: d["rule_vocab"]["PROMO_QUERY"].pop("regex_strong"))
        with self.assertRaises(ConfigError) as ctx:
            self._load(data)
        self.assertIn("regex_strong", str(ctx.exception))

    def test_missing_intent_vocab_is_fatal(self):
        data = _mutate(lambda d: d["rule_vocab"].pop("ORDER_QUERY"))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_priority_must_cover_seven_exactly(self):
        data = _mutate(lambda d: d["rule_vocab"].__setitem__(
            "priority", d["rule_vocab"]["priority"][:6]))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_missing_product_terms_key(self):
        data = _mutate(lambda d: d.pop("slot_hints"))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_wrong_type_is_fatal(self):
        data = _mutate(lambda d: d["thresholds"].__setitem__("conf_low", "0.6"))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_bad_json_is_fatal(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json ]")
        config_mod.DEFAULTS_PATH = path
        with self.assertRaises(ConfigError):
            load_config(env={})

    def test_missing_file_is_fatal(self):
        config_mod.DEFAULTS_PATH = os.path.join(tempfile.gettempdir(),
                                                "nlu-no-such-file.json")
        with self.assertRaises(ConfigError):
            load_config(env={})


class TestIllegalValuesFatal(unittest.TestCase):
    def setUp(self):
        self._orig = config_mod.DEFAULTS_PATH

    def tearDown(self):
        config_mod.DEFAULTS_PATH = self._orig

    def _load(self, data, env=None):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        config_mod.DEFAULTS_PATH = path
        return load_config(env=env if env is not None else {})

    def test_threshold_order_violation(self):
        data = _mutate(lambda d: d["thresholds"].__setitem__("conf_low", 0.95))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_conf_oos_above_conf_low(self):
        data = _mutate(lambda d: (d["thresholds"].__setitem__("conf_oos", 0.8),
                                 d["thresholds"].__setitem__("conf_low", 0.6)))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_negative_retry(self):
        data = _mutate(lambda d: d["thresholds"].__setitem__("llm_retry_max", -1))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_malformed_alias_entry(self):
        data = _mutate(lambda d: d["model_alias"].__setitem__(
            "entries", [{"alias": "只有别名"}]))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_money_key_is_rejected(self):
        """金额类键禁止进入 nlu 配置（DD-16 R-07 / IT-2.02 约束）。"""
        data = _mutate(lambda d: d["thresholds"].__setitem__("unit_price", 29999))
        with self.assertRaises(ConfigError):
            self._load(data)

    def test_amount_cents_key_is_rejected(self):
        data = _mutate(lambda d: d.setdefault("rule_vocab", {}).__setitem__(
            "amount_cents", 1))
        with self.assertRaises(ConfigError):
            self._load(data)


class TestOverlayAndEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _overlay(self, data) -> str:
        path = os.path.join(self.tmp, "overlay.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        return path

    def test_nlu_config_file_deep_merge(self):
        path = self._overlay({"thresholds": {"conf_low": 0.55},
                              "templates": {"clarify_generic": "改过的话术"}})
        cfg = load_config(env={"NLU_CONFIG_FILE": path})
        self.assertEqual(cfg.conf_low, 0.55)
        self.assertEqual(cfg.template("clarify_generic"), "改过的话术")
        # 深合并：未覆盖的兄弟键保持默认
        self.assertEqual(cfg.conf_rule_strong, 0.9)

    def test_env_override_threshold(self):
        cfg = load_config(env={"NLU_CONF_LOW": "0.7"})
        self.assertEqual(cfg.conf_low, 0.7)

    def test_env_takes_precedence_over_overlay(self):
        path = self._overlay({"thresholds": {"conf_low": 0.55}})
        cfg = load_config(env={"NLU_CONFIG_FILE": path, "NLU_CONF_LOW": "0.65"})
        self.assertEqual(cfg.conf_low, 0.65)

    def test_env_bad_value_is_fatal_not_fallback(self):
        with self.assertRaises(ConfigError):
            load_config(env={"NLU_CONF_LOW": "abc"})

    def test_env_missing_file_is_fatal(self):
        with self.assertRaises(ConfigError):
            load_config(env={"NLU_CONFIG_FILE": os.path.join(self.tmp, "nope.json")})

    def test_env_overlay_breaks_order_is_fatal(self):
        path = self._overlay({"thresholds": {"conf_oos": 0.8}})
        with self.assertRaises(ConfigError):
            load_config(env={"NLU_CONFIG_FILE": path})


class TestConfigAccessor(unittest.TestCase):
    def test_missing_path_raises(self):
        cfg = load_config(env={})
        with self.assertRaises(ConfigError):
            cfg.get("thresholds.no_such")
        with self.assertRaises(ConfigError):
            cfg.threshold("no_such")

    def test_unknown_template_raises(self):
        cfg = load_config(env={})
        with self.assertRaises(ConfigError):
            cfg.template("no_such_template")

    def test_alias_defaults_mark_q7(self):
        cfg = load_config(env={})
        self.assertEqual(cfg.alias_pending_question(), "Q7")


class TestConfigObjectIsReadOnlySnapshot(unittest.TestCase):
    def test_config_wraps_data_dict(self):
        with open(config_mod.DEFAULTS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = Config(data, source="unit")
        self.assertIs(cfg.raw(), data)
        self.assertEqual(cfg.source, "unit")


if __name__ == "__main__":
    unittest.main()
