import unittest
import os
from unittest.mock import patch, MagicMock
from src.rewards.composite_reward import composite_reward_v3_func, _judge_worker_count

class TestCompositeRewardV3(unittest.TestCase):
    @patch('src.rewards.composite_reward.get_mimo_judge')
    def test_composite_logic(self, mock_get_judge):
        # 建立 Mock Judge
        mock_judge = MagicMock()
        mock_get_judge.return_value = mock_judge
        
        # 定义用例
        # 1. 格式不合格 (缺少最终答案)
        # 2. 格式合格，exact match
        # 3. 格式合格，non-exact, 矛盾
        # 4. 格式合格，non-exact, 不矛盾 (语义得分 0.8)
        
        completions = [
            "<think>随便想想</think> 这里没有最终答案标识符", 
            "<think>推理</think>最终答案：阿司匹林", 
            "<think>推理</think>最终答案：布洛芬", 
            "<think>推理</think>最终答案：对乙酰氨基酚"
        ]
        standard_answers = ["阿司匹林", "阿司匹林", "阿司匹林", "阿司匹林"]
        questions = ["问题1", "问题2", "问题3", "问题4"]
        
        def mock_evaluate(q, ans, pred):
            if pred == "布洛芬":
                # 用例 3：矛盾
                return {"semantic_score": 0.0, "has_medical_contradiction": True}
            elif pred == "对乙酰氨基酚":
                # 用例 4：不矛盾，给 0.8 分
                return {"semantic_score": 0.8, "has_medical_contradiction": False}
            # 其他情况兜底
            return {"semantic_score": 0.0, "has_medical_contradiction": False}
            
        mock_judge.evaluate.side_effect = mock_evaluate
        
        rewards = composite_reward_v3_func(completions, standard_answers, question=questions)
        
        # 验证分数
        self.assertEqual(rewards[0], -0.25, "用例 1：格式不合格应该给 -0.25")
        self.assertEqual(rewards[1], 2.15, "用例 2：Exact Match 应该给 2.15")
        self.assertEqual(rewards[2], 0.0, "用例 3：医学矛盾应该给 0.0")
        self.assertAlmostEqual(rewards[3], 0.15 + 1.70 * 0.8, places=4, msg="用例 4：应该按公式计算")
        
        # 验证 API 调用次数 (只有 3 和 4 调用了 API)
        self.assertEqual(mock_judge.evaluate.call_count, 2, "API 只能被调用 2 次（只对 non-exact 的合规格式调用）")

    @patch.dict(os.environ, {"MIMO_JUDGE_MAX_WORKERS": "3"})
    def test_judge_worker_count_can_be_overridden(self):
        self.assertEqual(_judge_worker_count(), 3)

    @patch.dict(os.environ, {"MIMO_JUDGE_MAX_WORKERS": "invalid"})
    def test_judge_worker_count_falls_back_to_one(self):
        self.assertEqual(_judge_worker_count(), 1)

if __name__ == '__main__':
    unittest.main()
