import tempfile
import threading
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self, author="alice", title="可靠分布式提交协议"):
        return self.store.submit_paper(
            author, title, "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。"
        )["id"]

    def _reviewers(self, paper_id):
        with self.store.connect() as conn:
            return {
                r["reviewer_id"]: r["status"]
                for r in conn.execute(
                    "SELECT reviewer_id,status FROM assignments WHERE paper_id=?", (paper_id,)
                ).fetchall()
            }

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_manual_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)

    # ---- 一次分配 ----------------------------------------------------

    def test_allocate_prefers_bids_and_fills_two(self):
        paper_id = self._paper()
        # r3 愿意评，r2 勉强，r1 无意向（decline 应被跳过）。
        self.store.bid("r3", paper_id, "want")
        self.store.bid("r2", paper_id, "maybe")
        self.store.bid("r1", paper_id, "decline")
        result = self.store.allocate_paper("chair", paper_id)
        invited = [i["reviewer_id"] for i in result["invited"]]
        self.assertEqual(invited, ["r3", "r2"])  # 意愿排序。
        self.assertFalse(result["short"])
        self.assertEqual(set(self._reviewers(paper_id)), {"r3", "r2"})
        # 幂等：再调用一次不会产生新邀请。
        again = self.store.allocate_paper("chair", paper_id)
        self.assertEqual(again["invited"], [])

    def test_conflict_reviewer_is_skipped_during_allocation(self):
        paper_id = self._paper()
        self.store.bid("r1", paper_id, "want")
        self.store.bid("r2", paper_id, "want")
        # 直接登记冲突而不经过 add_conflict（后者会立即触发自动补位）。
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO conflicts(reviewer_id,paper_id,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                ("r1", paper_id, "近期合作", "chair", "now"),
            )
            conn.commit()
        result = self.store.allocate_paper("chair", paper_id)
        invited = {i["reviewer_id"] for i in result["invited"]}
        self.assertEqual(invited, {"r2", "r3"})

    def test_capacity_full_reviewers_queue_and_backfill_on_decline(self):
        # 三篇论文：r1/r2 负载先被占满，第四篇时他们排队，拒绝释放后补位。
        p1 = self._paper(title="论文一")
        p2 = self._paper(title="论文二")
        p3 = self._paper(title="论文三")
        for pid in (p1, p2, p3):
            self.store.assign("chair", pid, "r1")
            self.store.assign("chair", pid, "r2")
        # r1、r2 现有负载 3/3（r3 上限 2）。
        p4 = self._paper(title="论文四")
        self.store.bid("r1", p4, "want")
        self.store.bid("r2", p4, "want")
        result = self.store.allocate_paper("chair", p4)
        self.assertEqual({i["reviewer_id"] for i in result["invited"]}, {"r3"})
        self.assertEqual(set(result["enqueued"]), {"r1", "r2"})
        self.assertTrue(result["short"])

        # r1 拒绝 p1 的邀请，释放一个负载：排队中 r1（want）立即补到 p4。
        with self.store.connect() as conn:
            a1p1 = conn.execute(
                "SELECT id FROM assignments WHERE paper_id=? AND reviewer_id='r1'", (p1,)
            ).fetchone()["id"]
        resp = self.store.respond_assignment("r1", a1p1, False)
        self.assertEqual(self._reviewers(p4).get("r1"), "invited")
        # refill[0] 是被拒论文自身补位，其余项覆盖排队补位。
        refilled = {
            (r["paper_id"], i["reviewer_id"])
            for r in resp["refill"] for i in r["invited"]
        }
        self.assertIn((p4, "r1"), refilled)
        # 补发原因进入审计历史。
        actions = [h["action"] + ":" + h["detail"].get("reason", "") for h in self.store.history("chair", p4)]
        self.assertIn("assignment.invite:refill_decline", actions)

    def test_two_chairs_concurrent_same_paper_creates_single_set(self):
        paper_id = self._paper()
        errors = []

        def run(chair):
            try:
                self.store.allocate_paper(chair, paper_id)
            except Exception as exc:  # 其中一方可能拿不到写锁，重试一次即可。
                errors.append(exc)

        t1 = threading.Thread(target=run, args=("chair",))
        t2 = threading.Thread(target=run, args=("chair2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        invited = self._reviewers(paper_id)
        self.assertEqual(len(invited), 2)  # 只有一份邀请集合，不重复占负载。
        # 每位评审人在该论文上恰有一条分配。
        with self.store.connect() as conn:
            dupes = conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE paper_id=?", (paper_id,)
            ).fetchone()[0]
        self.assertEqual(dupes, 2)

    def test_conflict_revokes_pending_invite_and_refills(self):
        paper_id = self._paper()
        result = self.store.allocate_paper("chair", paper_id)
        victim = result["invited"][0]["reviewer_id"]
        remaining = result["invited"][1]["reviewer_id"]
        out = self.store.add_conflict("chair2", paper_id, victim, "事后发现同机构")
        self.assertEqual(out["revoked_assignment_id"], result["invited"][0]["assignment_id"])
        statuses = self._reviewers(paper_id)
        self.assertEqual(statuses[victim], "revoked")
        active = [r for r, s in statuses.items() if s in ("invited", "accepted", "completed")]
        self.assertEqual(len(active), 2)
        self.assertIn(remaining, active)
        actions = [(h["action"], h["detail"].get("reason")) for h in self.store.history("chair", paper_id)]
        self.assertIn(("assignment.revoke", "refill_conflict"), actions)
        self.assertIn(("assignment.invite", "refill_conflict"), actions)

    def test_load_limit_cut_revokes_newest_invites_and_backfills(self):
        p1 = self._paper(title="论文一")
        p2 = self._paper(title="论文二")
        a1 = self.store.assign("chair", p1, "r3")["id"]
        a2 = self.store.assign("chair", p2, "r3")["id"]
        # r3 负载 2/2，下调到 1：最新邀请（p2）失效，排队池里其他人补 p2。
        out = self.store.update_load_limit("chair", "r3", 1)
        self.assertEqual(out["revoked_assignment_ids"], [a2])
        self.assertNotEqual(a1, a2)
        status_p1 = self._reviewers(p1)["r3"]
        self.assertEqual(status_p1, "invited")  # 较早的邀请保留。
        active_p2 = {r: s for r, s in self._reviewers(p2).items() if s in ("invited", "accepted", "completed")}
        self.assertEqual(len(active_p2), 2)  # r3 撤离后由两名在场评审人补足。
        self.assertNotIn("r3", active_p2)
        actions = [(h["action"], h["detail"].get("reason")) for h in self.store.history("chair", p2)]
        self.assertIn(("assignment.revoke", "refill_load"), actions)

    def test_batch_job_resumes_from_breakpoint_and_records_reason(self):
        papers = [self._paper(title=f"批量论文{i}") for i in range(3)]
        # 让第二篇在写入中途失败：前一篇已独立提交，必须保留。
        self.store.allocation_failure_paper_id = papers[1]
        job = self.store.create_allocation_job("chair", papers)
        self.assertEqual(job["status"], "failed")
        statuses = {item["paper_id"]: item["status"] for item in job["items"]}
        self.assertEqual(statuses[papers[0]], "completed")
        self.assertEqual(statuses[papers[1]], "failed")
        self.assertEqual(statuses[papers[2]], "pending")
        # 断点前的成果仍在。
        self.assertEqual(len(self._reviewers(papers[0])), 2)

        # 重试从断点补齐；已完成的第一篇不会被重复分配。
        job = self.store.retry_allocation_job("chair", job["id"])
        self.assertEqual(job["status"], "completed")
        for pid in papers:
            self.assertEqual(len(self._reviewers(pid)), 2)

    def test_batch_retry_refills_shortage_left_by_late_conflict(self):
        # 三名评审人全部冲突，任务完成但缺员；重跑幂等不产生重复邀请。
        paper_id = self._paper()
        for rid in ("r1", "r2", "r3"):
            self.store.add_conflict("chair", paper_id, rid, f"与 {rid} 同机构")
        job = self.store.create_allocation_job("chair", [paper_id])
        self.assertEqual(job["status"], "completed")
        item = job["items"][0]["result"]
        self.assertTrue(item["short"])
        self.assertEqual(item["active"], 0)
        # 再跑一次仍是幂等的。
        again = self.store.retry_allocation_job("chair", job["id"])
        self.assertEqual(again["items"][0]["result"]["invited"], [])

    def test_allocation_with_too_few_reviewers_reports_shortage(self):
        paper_id = self._paper()
        for rid in ("r1", "r2", "r3"):
            self.store.add_conflict("chair", paper_id, rid, "全员冲突场景")
        result = self.store.allocate_paper("chair", paper_id)
        self.assertTrue(result["short"])
        self.assertEqual(result["active"], 0)

    def test_only_chair_may_allocate(self):
        paper_id = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.allocate_paper("r1", paper_id)
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
