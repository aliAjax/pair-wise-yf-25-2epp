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

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

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

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
        self.assertEqual(ctx.exception.status, 403)

    def _active(self, paper_id):
        with self.store.connect() as conn:
            return [r["reviewer_id"] for r in conn.execute(
                "SELECT reviewer_id FROM assignments WHERE paper_id=? AND status != 'expired' ORDER BY id",
                (paper_id,),
            )]

    def test_auto_assign_fills_two_reviewers(self):
        p1 = self._paper()
        p2 = self.store.submit_paper("bob", "基于 Raft 的配置变更研究", "本文研究 Raft 共识算法中的配置变更流程，并给出正确性证明与实验评估。")["id"]
        summary = self.store.auto_assign("chair")
        self.assertEqual(len(summary["invited"]), 4)
        self.assertEqual(len(self._active(p1)), 2)
        self.assertEqual(len(self._active(p2)), 2)

    def test_auto_assign_prefers_willing_bidders(self):
        paper_id = self._paper()
        self.store.bid("r1", paper_id, "maybe")
        self.store.bid("r2", paper_id, "want")
        self.store.bid("r3", paper_id, "want")
        self.store.auto_assign("chair", paper_id)
        self.assertEqual(set(self._active(paper_id)), {"r2", "r3"})

    def test_auto_assign_skips_conflicts(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        self.store.auto_assign("chair", paper_id)
        self.assertEqual(set(self._active(paper_id)), {"r2", "r3"})

    def test_queue_fills_when_capacity_frees(self):
        paper_id = self._paper()
        for rid in ("r1", "r2", "r3"):
            self.store.set_load_limit("chair", rid, 0)
        self.store.auto_assign("chair", paper_id)
        self.assertEqual(self._active(paper_id), [])
        with self.store.connect() as conn:
            queued = [r["reviewer_id"] for r in conn.execute(
                "SELECT reviewer_id FROM assignment_queue WHERE paper_id=? AND status='queued'", (paper_id,)
            )]
        self.assertEqual(set(queued), {"r1", "r2", "r3"})
        self.store.set_load_limit("chair", "r1", 3)
        self.store.set_load_limit("chair", "r2", 3)
        self.assertEqual(set(self._active(paper_id)), {"r1", "r2"})

    def test_concurrent_auto_assign_no_duplicate_invites(self):
        paper_id = self._paper()
        errors = []
        def worker():
            try:
                self.store.auto_assign("chair", paper_id)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT reviewer_id, COUNT(*) AS n FROM assignments WHERE paper_id=? GROUP BY reviewer_id",
                (paper_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["n"] == 1 for r in rows))

    def test_conflict_expires_pending_invite_and_backfills(self):
        paper_id = self._paper()
        self.store.auto_assign("chair", paper_id)
        invited = self._active(paper_id)
        self.assertEqual(len(invited), 2)
        conflicted = invited[0]
        self.store.add_conflict("chair", paper_id, conflicted, "合作作者关系")
        with self.store.connect() as conn:
            expired = [r["reviewer_id"] for r in conn.execute(
                "SELECT reviewer_id FROM assignments WHERE paper_id=? AND status='expired'", (paper_id,)
            )]
        self.assertEqual(expired, [conflicted])
        active = self._active(paper_id)
        self.assertEqual(len(active), 2)
        self.assertNotIn(conflicted, active)
        history = self.store.history("chair", paper_id)
        self.assertTrue(any(h["action"] == "assignment.expire" and h["detail"].get("reason") == "conflict_added"
                            for h in history))
        self.assertTrue(any(h["action"] == "assignment.auto_invite"
                            and h["detail"].get("reason", "").startswith("backfill:conflict_added")
                            for h in history))

    def test_load_limit_decrease_expires_pending_invite_and_backfills(self):
        paper_id = self._paper()
        self.store.auto_assign("chair", paper_id)
        target = self._active(paper_id)[0]
        self.store.set_load_limit("chair", target, 0)
        with self.store.connect() as conn:
            expired = [r["reviewer_id"] for r in conn.execute(
                "SELECT reviewer_id FROM assignments WHERE paper_id=? AND reviewer_id=? AND status='expired'",
                (paper_id, target),
            )]
        self.assertEqual(expired, [target])
        self.assertEqual(len(self._active(paper_id)), 2)
        history = self.store.history("chair", paper_id)
        self.assertTrue(any(h["action"] == "assignment.expire"
                            and h["detail"].get("reason") == "load_limit_decreased" for h in history))

    def test_auto_assign_resumes_after_failure(self):
        p1 = self._paper()
        p2 = self.store.submit_paper("bob", "可验证的联邦学习", "本文研究联邦学习中的可验证性问题，提出一种防篡改的聚合协议并给出安全性分析。")["id"]
        with self.assertRaises(BusinessError) as ctx:
            self.store.auto_assign("chair", fail_after=1)
        self.assertEqual(ctx.exception.code, "simulated_failure")
        done = [pid for pid in (p1, p2) if len(self._active(pid)) == 2]
        self.assertEqual(len(done), 1)
        self.store.auto_assign("chair")
        self.assertEqual(len(self._active(p1)), 2)
        self.assertEqual(len(self._active(p2)), 2)


if __name__ == "__main__":
    unittest.main()
