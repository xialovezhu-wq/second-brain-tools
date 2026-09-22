"""Date regression fixtures only; this module never reads the personal vault."""

import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'fixture_lint', Path(__file__).with_name('second_brain_lint.py')
)
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)


class DateLintTests(unittest.TestCase):
    def test_invalid_dates_are_findings_and_other_checks_continue(self):
        with tempfile.TemporaryDirectory(prefix='second-brain-lint-fixture-') as tmp:
            vault = Path(tmp)
            brain = vault / '第二大脑'
            daily = brain / '每日记录'
            daily.mkdir(parents=True)
            (daily / '2026-02-30.md').write_text('# 2026-02-30\n')
            (daily / '2024-02-29.md').write_text('# 2024-02-29\n')
            index = brain / '索引'
            index.mkdir()
            (index / '待确认问题.md').write_text(
                '最后更新：2026-13-01\n'
                '| Q-fixture | test | 2026-02-31 | 待确认 |\n'
                '| Q-future | test | 2026-99-99 | 待确认 |\n'
            )
            (brain / 'invalid.json').write_text('{broken')
            with patch.object(lint, 'VAULT', vault), patch.object(lint, 'SECOND_BRAIN', brain), \
                    patch.object(lint, 'TODAY', date(2026, 9, 7)):
                findings, summary = lint.run_lint()
            invalid = [item for item in findings if item['code'] == 'INVALID_DATE']
            self.assertEqual(len(invalid), 4)
            self.assertTrue(any(item['code'] == 'INVALID_JSON' for item in findings))
            self.assertFalse(any('2024-02-29' in item['detail'] for item in invalid))
            self.assertTrue(summary['read_only'])
            self.assertEqual((daily / '2026-02-30.md').read_text(), '# 2026-02-30\n')


if __name__ == '__main__':
    unittest.main()
