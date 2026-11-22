"""转件领域逻辑测试，仅依赖标准库，可直接 `python3 -m unittest` 运行。"""

import copy
import hashlib
import json
import unittest

from src.services.domain import SPEC, build_report
from src.services.transfers import (TransferError, parse_request,
                                    plan_transfer, prepare_reports)


def payload(records, batch_no='B-DEMO', run_bytes=4096, fan_in=2):
    return {'batch_no': batch_no, 'run_bytes': run_bytes, 'fan_in': fan_in,
            'records': records}


def item(item_id, accession_no, title=None):
    return {'item_id': item_id, 'accession_no': accession_no,
            'title': title or f'题名-{item_id}'}


CASE_A = payload([
    item('A1', 'GJ-2026-1'), item('A2', 'GJ-2026-2'),
    item('A3', 'GJ-2026-3'), item('A4', 'GJ-2026-4'),
], batch_no='BATCH-A')

CASE_B = payload([
    item('B1', 'QP-2025-1'), item('B2', 'QP-2025-2'),
    item('B3', 'QP-2025-3'),
], batch_no='BATCH-B', run_bytes=2048)


class ParseRequestTests(unittest.TestCase):
    def base(self, **over):
        data = {'handover_no': 'HJ-01', 'from_case_id': 1, 'to_case_id': 2,
                'from_item_ids': ['A1'], 'to_item_ids': []}
        data.update(over)
        return data

    def test_valid_one_way(self):
        parsed = parse_request(self.base())
        self.assertEqual(parsed['handover_no'], 'HJ-01')
        self.assertEqual(parsed['from_item_ids'], ['A1'])

    def test_same_case_rejected(self):
        with self.assertRaises(TransferError):
            parse_request(self.base(to_case_id=1))

    def test_handover_required(self):
        with self.assertRaises(TransferError):
            parse_request(self.base(handover_no='  '))

    def test_handover_too_long(self):
        with self.assertRaises(TransferError):
            parse_request(self.base(handover_no='X' * 65))

    def test_at_least_one_item(self):
        with self.assertRaises(TransferError):
            parse_request(self.base(from_item_ids=[], to_item_ids=[]))

    def test_duplicate_item_rejected(self):
        with self.assertRaises(TransferError):
            parse_request(self.base(from_item_ids=['A1', 'A1']))

    def test_bool_case_id_rejected(self):
        with self.assertRaises(TransferError):
            parse_request(self.base(from_case_id=True))

    def test_blank_item_rejected(self):
        with self.assertRaises(TransferError):
            parse_request(self.base(from_item_ids=['  ']))

    def test_not_object(self):
        with self.assertRaises(TransferError):
            parse_request([])


class PlanTransferTests(unittest.TestCase):
    def test_one_way_move(self):
        request = parse_request({'handover_no': 'HJ-01', 'from_case_id': 7,
                                 'to_case_id': 8, 'from_item_ids': ['A2', 'A3'],
                                 'to_item_ids': []})
        plan = plan_transfer(request, copy.deepcopy(CASE_A), copy.deepcopy(CASE_B))
        new_a = plan['from']['payload']['records']
        new_b = plan['to']['payload']['records']
        # 留批件保持原顺位，转出件消失。
        self.assertEqual([r['item_id'] for r in new_a], ['A1', 'A4'])
        # 接收批次：原件在前，转入件按对方清册顺位追加在后。
        self.assertEqual([r['item_id'] for r in new_b], ['B1', 'B2', 'B3', 'A2', 'A3'])

    def test_swap_both_directions(self):
        request = parse_request({'handover_no': 'HJ-02', 'from_case_id': 7,
                                 'to_case_id': 8, 'from_item_ids': ['A1'],
                                 'to_item_ids': ['B1', 'B2']})
        plan = plan_transfer(request, copy.deepcopy(CASE_A), copy.deepcopy(CASE_B))
        self.assertEqual([r['item_id'] for r in plan['from']['payload']['records']],
                         ['A2', 'A3', 'A4', 'B1', 'B2'])
        self.assertEqual([r['item_id'] for r in plan['to']['payload']['records']],
                         ['B3', 'A1'])

    def test_missing_item_fails_and_inputs_untouched(self):
        a_before = copy.deepcopy(CASE_A)
        b_before = copy.deepcopy(CASE_B)
        request = parse_request({'handover_no': 'HJ-03', 'from_case_id': 7,
                                 'to_case_id': 8, 'from_item_ids': ['NOPE'],
                                 'to_item_ids': []})
        with self.assertRaises(TransferError):
            plan_transfer(request, CASE_A, CASE_B)
        # 纯函数处理，输入清册绝不被改写。
        self.assertEqual(CASE_A, a_before)
        self.assertEqual(CASE_B, b_before)

    def test_rebuild_keeps_batch_parameters(self):
        request = parse_request({'handover_no': 'HJ-04', 'from_case_id': 7,
                                 'to_case_id': 8, 'from_item_ids': ['A1'],
                                 'to_item_ids': []})
        plan = plan_transfer(request, copy.deepcopy(CASE_A), copy.deepcopy(CASE_B))
        self.assertEqual(plan['from']['payload']['batch_no'], 'BATCH-A')
        self.assertEqual(plan['to']['payload']['batch_no'], 'BATCH-B')
        self.assertEqual(plan['to']['payload']['run_bytes'], 2048)

    def test_incoming_records_keep_identity_fields(self):
        request = parse_request({'handover_no': 'HJ-05', 'from_case_id': 7,
                                 'to_case_id': 8, 'from_item_ids': ['A2'],
                                 'to_item_ids': []})
        plan = plan_transfer(request, copy.deepcopy(CASE_A), copy.deepcopy(CASE_B))
        moved = plan['to']['payload']['records'][-1]
        self.assertEqual(moved['accession_no'], 'GJ-2026-2')
        self.assertEqual(moved['title'], '题名-A2')


class PrepareReportsTests(unittest.TestCase):
    def _request(self, **over):
        data = {'handover_no': 'HJ-10', 'from_case_id': 7, 'to_case_id': 8,
                'from_item_ids': ['A1'], 'to_item_ids': ['B1']}
        data.update(over)
        return parse_request(data)

    def test_two_reports_built_together(self):
        plan = plan_transfer(self._request(), copy.deepcopy(CASE_A), copy.deepcopy(CASE_B))
        prepared = prepare_reports(plan)
        for side in ('from', 'to'):
            report = prepared[side]['report']
            self.assertTrue(report['sections'][0]['rows'])
            self.assertEqual(report['metrics']['上架件数'],
                             len(prepared[side]['payload']['records']))

    def test_swap_order_reflects_new_membership(self):
        plan = plan_transfer(self._request(), copy.deepcopy(CASE_A), copy.deepcopy(CASE_B))
        prepared = prepare_reports(plan)
        a_ids = {row['馆藏件号'] for row in prepared['from']['report']['sections'][0]['rows']}
        b_ids = {row['馆藏件号'] for row in prepared['to']['report']['sections'][0]['rows']}
        self.assertEqual(a_ids, {'A2', 'A3', 'A4', 'B1'})
        self.assertEqual(b_ids, {'B2', 'B3', 'A1'})

    def test_any_report_failure_aborts_whole_transfer(self):
        # 让接收批次的预算小到放不下转入件，第二份顺序单必然失败。
        tight_b = payload([item('B1', 'QP-2025-1')], run_bytes=8)
        plan = plan_transfer(self._request(), copy.deepcopy(CASE_A), copy.deepcopy(tight_b))
        with self.assertRaises(ValueError):
            prepare_reports(plan)

    def test_deterministic_digest_across_repeats(self):
        plan = plan_transfer(self._request(), copy.deepcopy(CASE_A), copy.deepcopy(CASE_B))
        first = prepare_reports(copy.deepcopy(plan))
        second = prepare_reports(copy.deepcopy(plan))
        for side in ('from', 'to'):
            d1 = hashlib.sha256(json.dumps(
                first[side]['report'], ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
            d2 = hashlib.sha256(json.dumps(
                second[side]['report'], ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
            self.assertEqual(d1, d2)


class DemoRegressionTests(unittest.TestCase):
    """重构 build_report 后，演示批次的顺序与读写指标必须保持不变。"""

    def test_demo_output_matches_business_spec(self):
        report = build_report(SPEC['sample'])
        order = [row['馆藏件号'] for row in report['sections'][0]['rows']]
        self.assertEqual(order,
                         ['BX-05', 'BX-09', 'BX-02', 'BX-06', 'BX-07',
                          'BX-04', 'BX-08', 'BX-01', 'BX-03'])
        metrics = report['metrics']
        self.assertEqual(metrics['初始磁盘段数'], 5)
        self.assertEqual(metrics['归并轮数'], 3)
        self.assertEqual(metrics['峰值归并输入段数'], 2)
        self.assertEqual(metrics['磁盘读取字节'], 1494)
        self.assertEqual(metrics['磁盘写入字节'], 1494)


if __name__ == '__main__':
    unittest.main()
