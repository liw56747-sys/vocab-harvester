"""测试专用外部审核实现；不参与应用打包。"""
from src.adapter.base import WorkflowAdapter
from src.common.models import ExtractedKeyword, WorkflowResult


class FakeAuditAdapter(WorkflowAdapter):
    async def submit(self, posts, *, context=None):
        self.posts = posts
        self.context = context
        return 'test-audit-task'

    async def poll_result(self, task_id):
        return WorkflowResult(task_id=task_id, keywords=[ExtractedKeyword(
            candidate='测试候选', source_id=self.posts[0].post_id, score=50,
            action='need_human_review')])
