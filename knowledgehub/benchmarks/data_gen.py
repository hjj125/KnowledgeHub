"""合成数据生成器（§15.7）：模板化中文笔记，固定随机种子可复现。

真实评测集用于检索质量实验（exp4-B 使用同义改写构造）；本生成器面向索引、
吞吐与扩展性实验——句子为模板组合，语义区分度有限，不适合作为检索质量评测资料
（设计文档 §15.7 明示该限制）。

用法：
    from benchmarks.data_gen import gen_notes, gen_graph_edges
    notes = gen_notes(10_000, seed=42)
"""

import random
import uuid
from datetime import datetime, timedelta, timezone

TOPICS = ["操作系统", "计算机网络", "数据结构", "数据库系统", "分布式系统", "机器学习"]

# 每主题：标题词池 + 陈述句模板（{} 槽位填充次级词）
TITLE_WORDS = {
    "操作系统": ["进程管理", "线程调度", "内存管理", "文件系统", "死锁处理", "中断机制", "虚拟内存", "设备驱动"],
    "计算机网络": ["TCP 协议", "UDP 协议", "路由算法", "拥塞控制", "HTTP 协议", "DNS 解析", "TLS 握手", "ARP 协议"],
    "数据结构": ["二叉树", "哈希表", "堆与优先队列", "图的遍历", "并查集", "跳表", "B+ 树", "红黑树"],
    "数据库系统": ["事务与 ACID", "索引原理", "查询优化", "日志与恢复", "并发控制", "分库分表", "MVCC 机制", "主从复制"],
    "分布式系统": ["一致性哈希", "Raft 协议", "Paxos 共识", "分布式锁", "消息队列", "限流熔断", "服务注册发现", "分布式事务"],
    "机器学习": ["线性回归", "决策树", "神经网络", "卷积网络", "注意力机制", "梯度下降", "过拟合与正则化", "聚类算法"],
}

SENTENCES = {
    "操作系统": [
        "进程是{kw}中资源分配的基本单位，拥有独立的虚拟地址空间。",
        "线程是CPU调度的基本单位，同一进程内的线程共享{kw}相关的地址空间与文件描述符。",
        "{kw}的核心问题是在并发环境下保证数据一致性与系统吞吐的平衡。",
        "当{kw}出现异常时，内核会通过信号机制通知进程并执行默认处理动作。",
        "在{kw}设计中，时间片轮转与优先级队列是两种常见的调度策略。",
        "缓存一致性协议保证了多核{kw}场景下各核心看到的数据视图一致。",
    ],
    "计算机网络": [
        "{kw}通过三次握手建立连接，四次挥手释放连接。",
        "在{kw}场景下，滑动窗口机制同时兼顾了传输效率与拥塞控制。",
        "{kw}的可靠性依赖序列号确认与超时重传机制。",
        "当网络出现拥塞时，{kw}会进入快速重传与拥塞避免阶段。",
        "{kw}在局域网内通过广播方式解析地址映射关系。",
        "报文经过{kw}处理时会逐跳修改链路层地址，网络层地址保持不变。",
    ],
    "数据结构": [
        "{kw}的查找、插入与删除的平均时间复杂度均为 O(1) 到 O(log n)。",
        "对{kw}进行中序遍历可以得到有序序列。",
        "{kw}通过旋转操作维持树的平衡，避免退化成链表。",
        "在{kw}中，负载因子决定了扩容时机与冲突概率。",
        "{kw}适合海量数据的外存储场景，扇出越大树高越低。",
        "路径压缩与按秩合并是优化{kw}的两种关键手段。",
    ],
    "数据库系统": [
        "{kw}通过预写日志保证崩溃后数据可恢复。",
        "在{kw}实现中，B+ 树叶子节点通过链表相连以支持范围扫描。",
        "{kw}的隔离级别决定了脏读、不可重复读与幻读是否可能出现。",
        "复合索引的字段顺序直接影响{kw}的查询效率。",
        "多版本并发控制让{kw}的读写操作互不阻塞。",
        "当{kw}的命中率下降时，查询优化器会改用全表扫描。",
    ],
    "分布式系统": [
        "{kw}通过虚拟节点映射改善了节点扩缩容时的数据迁移成本。",
        "在{kw}协议中，领导者选举与日志复制是两个核心子问题。",
        "{kw}要求多数派确认后才能提交，保证已提交项不会丢失。",
        "实现{kw}时需要处理令牌续期与锁失效的边界情况。",
        "{kw}通过削峰填谷解耦了生产者与消费者的处理能力差异。",
        "超时与重试是{kw}容错设计的基础，但需要配合幂等性保证。",
    ],
    "机器学习": [
        "{kw}通过最小化损失函数拟合训练数据的分布规律。",
        "在{kw}训练中，学习率是最敏感的超参数之一。",
        "{kw}以信息增益或基尼系数作为分裂准则。",
        "{kw}通过共享权重大幅减少了模型的参数量。",
        "正则化项能够抑制{kw}在训练集上的过拟合倾向。",
        "{kw}的梯度消失问题可以通过残差连接或门控单元缓解。",
    ],
}

TAG_POOL = ["核心概念", "期末复习", "易错点", "实验记录", "课程笔记", "面试准备", "公式推导", "经典例题"]

# 每主题的独特场景词：注入案例句制造"语义级可区分"的专有内容。
# （曾尝试纯数字编号区分——实测仅数字不同的文本 dense 余弦 0.87，语义嵌入不做 ID 级区分）
SCENARIOS = {
    "操作系统": ["缺页中断频发", "上下文切换开销激增", "死锁检测触发", "缓冲区溢出告警", "系统调用阻塞",
                "时钟中断丢失", "页面置换抖动", "僵尸进程堆积", "信号量竞争激烈", "管道阻塞超时",
                "线程池耗尽", "内存碎片整理", "文件句柄泄漏", "内核态占比过高", "中断风暴"],
    "计算机网络": ["三次握手超时", "拥塞窗口骤降", "丢包重传风暴", "DNS 解析污染", "TLS 版本不兼容",
                  "路由环路抖动", "NAT 端口耗尽", "MTU 分片丢失", "ARP 欺骗告警", "长连接空转断开",
                  "滑动窗口收缩", "带宽打满限速", "连接队列溢出", "组播风暴扩散", "代理链路超时"],
    "数据结构": ["哈希冲突链延长", "树旋转失衡", "堆上溢越界", "图遍历栈溢出", "并查集退化线性",
                "跳表层数倾斜", "B+ 树页分裂", "红黑树染色频繁", "环形缓冲区回绕", "递归深度超限",
                "缓存行伪共享", "尾递归展开失败", "内存对齐浪费", "迭代器失效崩溃", "负载因子越界扩容"],
    "数据库系统": ["慢查询堆积", "死锁检测升级", "主从延迟扩大", "索引失效回表", "事务锁等待超时",
                  "日志盘写满", "连接池耗尽", "缓存穿透雪崩", "大事务回滚超时", "统计信息过期",
                  "执行计划劣化", "分片键倾斜", "检查点拖慢写入", "快照过久导致膨胀", "备份锁表卡顿"],
    "分布式系统": ["脑裂误选主", "时钟漂移越界", "重试风暴放大", "熔断误触发", "限流阈值击穿",
                  "消息积压溢出", "幂等键冲突", " gossip 同步滞后", "热点分片迁移", "优雅下线超时",
                  "配置推送丢失", "服务雪崩连锁", "幂等重放冲突", "分布式锁惊群", "对账差异扩大"],
    "机器学习": ["梯度爆炸发散", "损失函数震荡", "过拟合拐点", "学习率过大跳摆", "类别极度不均衡",
                "特征泄漏告警", "批次统计漂移", "注意力头坍缩", "梯度消失断流", "数据增强过猛",
                "标签噪声升高", "早停判断失误", "验证集拟合", "嵌入维度塌缩", "采样偏差放大"],
}

SEED_SENTENCE = "本段落用于填充实验语料，包含与主题相关的陈述句以保证分块器产出稳定。"


def gen_notes(n: int, seed: int = 42, kb_id: str | None = None, owner_id: str = "bench") -> list[dict]:
    """生成 n 篇笔记文档（匹配 notes 集合 Schema，含 _id/时间递增/标签/版本）。

    每篇注入一条"案例细节句"（含唯一编号）模拟真实语料的专有内容——
    模板句在语料内大量重复，没有唯一内容则任何检索都无法区分目标 chunk。
    """
    rng = random.Random(seed)
    base_time = datetime(2025, 9, 1, tzinfo=timezone.utc)
    notes: list[dict] = []
    for i in range(n):
        topic = TOPICS[i % len(TOPICS)]
        words = TITLE_WORDS[topic]
        title = f"{topic}·{words[(i // len(TOPICS)) % len(words)]}（{i}）"
        paragraph_count = rng.randint(2, 5)
        scenario = rng.choice(SCENARIOS[topic])  # 语义级独特内容（exp4b 片段回源任务的真值锚点）
        paragraphs = []
        for p_idx in range(paragraph_count):
            sent_count = rng.randint(2, 4)
            sentences = [rng.choice(SENTENCES[topic]).format(kw=rng.choice(words)) for _ in range(sent_count)]
            if p_idx == 0:  # 首段注入案例细节句
                sentences.append(
                    f"典型案例：在{scenario}的场景下，{rng.choice(words)}的相关指标出现了显著变化，"
                    f"现场快照与处理记录已归档备查。"
                )
            paragraphs.append("".join(sentences) + SEED_SENTENCE)
        content = "\n\n".join(paragraphs)
        properties = {"bench_scenario": scenario}
        notes.append(
            {
                "_id": str(uuid.uuid4()),
                "owner_id": owner_id,
                "kb_id": kb_id or f"bench-kb-{rng.randint(1, 10)}",
                "title": title,
                "content_md": content,
                "tags": rng.sample(TAG_POOL, rng.randint(1, 3)),
                "properties": properties,
                "linked_note_ids": [],
                "content_version": 1,
                "index_status": "indexed",
                "deleted_at": None,
                "created_at": base_time + timedelta(seconds=i),
                "updated_at": base_time + timedelta(seconds=i),
            }
        )
    return notes


def gen_graph_edges(notes: list[dict], min_degree: int = 3, max_degree: int = 8,
                    seed: int = 42) -> list[tuple[str, str]]:
    """在同一集合内按 3—8 度生成有向链接边（源→目标），供图实验使用。"""
    rng = random.Random(seed)
    ids = [n["_id"] for n in notes]
    edges: list[tuple[str, str]] = []
    for src in ids:
        degree = rng.randint(min_degree, max_degree)
        targets = rng.sample(ids, min(degree + 1, len(ids)))
        for dst in targets:
            if dst != src:
                edges.append((src, dst))
    return edges


def gen_queries(n: int, notes: list[dict], seed: int = 42) -> list[str]:
    """从语料抽词构造查询串（仅用于性能实验，不适合质量评测）。"""
    rng = random.Random(seed)
    queries = []
    for _ in range(n):
        note = rng.choice(notes)
        topic = note["title"].split("·")[0]
        words = TITLE_WORDS.get(topic, ["系统"])
        queries.append(rng.choice(words))
    return queries
