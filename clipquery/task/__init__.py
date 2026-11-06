"""定时任务（后台周期性作业）。

当前任务：
    - clipquery.task.cgtn: 每小时扫 CGTN transcript 列表增量入库

每个任务都对外暴露 run_once() 函数（一次性执行，便于手动触发/测试）
和 __main__ 入口（APScheduler 常驻）。
"""
