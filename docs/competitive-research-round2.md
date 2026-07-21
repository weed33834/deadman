# 国际身后事 / 终活 / 遗产规划类 AI 与数字化产品深度调研报告（第二轮）

> 调研时间：2026-07-21
> 调研对象：15 家国际同类产品（Cake / Everplans / Lantern / Empathy / Tomorrow / Fabric / Nolo WillMaker / Trust & Will / GoodTrust / FreeWill / Better Place Forests / eFuneral / Toast / Afterword / Willing）
> 调研方法：WebSearch + WebFetch 官网与三方评测原文，未使用记忆补全
> 报告目的：为 deadman（身后事 + 医疗导航多智能体平台）输出可借鉴功能清单与差异化机会判断

---

## 一、执行摘要

下表对 15 家产品做一句话定位 + 综合评分（满分 10，权重：AI 智能化 30% / 功能完整度 25% / 商业模式可持续性 20% / 隐私合规 15% / 与 deadman 可借鉴度 10%）。

| # | 产品 | 一句话定位 | 评分 | 最值得 deadman 借鉴的一点 |
|---|---|---|---|---|
| 1 | Cake | 美国最大免费终活规划教育平台，内容 + 表单驱动 | 7.5 | 五分区结构化规划 + 海量教育内容 |
| 2 | Everplans | 老牌数字保险箱 + "Deputies" 受托人触发机制 | 7.8 | Deputies 等待期 + 文档过期提醒 |
| 3 | Lantern | 丧亲后流程导航 + 8 类通知信函生成器 | 8.0 | 结构化通知信函模板库 |
| 4 | Empathy | AI + 人工 Care Manager 双轨丧亲陪伴，B2B2C 走保险/雇主渠道 | 9.0 | AI 悼文 + 自动云账户关闭 + 24/7 人工 Care Manager |
| 5 | Tomorrow | 移动端免费遗嘱 + 信托 + 保险交叉销售 | 6.5 | 受益人 designation 复核提醒 |
| 6 | Fabric (by Gerber Life) | 真正免费的移动端遗嘱 + 保险交叉销售 | 6.8 | 5 分钟极简 onboarding + Fabric Vault |
| 7 | Nolo WillMaker | 50 年历史的桌面 + 在线遗嘱软件，全家桶授权 | 7.2 | "Information for Caregivers" 执行人指南 + household license |
| 8 | Trust & Will | 法律文档为核 + EstateOS 会员制 AI 助手 | 8.3 | Plan Strength Score + Mobile Notary + Trust Funding 帮助 |
| 9 | GoodTrust | 数字遗产执行 + 字面意义的 "Dead Man Switch" 自动触发 | 8.5 | Dead Man Switch 自动触发机制（直接对标 deadman） |
| 10 | FreeWill | 100% 免费遗嘱 + 2400+ 非营利组织分账 | 8.0 | 慈善遗赠 nonprofit 分账模式 |
| 11 | Better Place Forests | 保护性森林 + 纪念树葬（绿色殡葬） | 7.0 | Memorial Tree 永久访问权 + 保育绑定 |
| 12 | eFuneral | 殡葬比价 + B2B 殡仪馆线索生成平台 | 6.5 | abandoned cart 67% 转化线索机制 |
| 13 | Toast (ToastPal) | AI 悼文 / 悼词生成器，单点工具 | 6.0 | AI 悼文生成的 prompt 结构 |
| 14 | Afterword / after.com | 殡仪馆 SaaS（B2B）+ 透明定价火化 O2O（B2C）双业务 | 7.5 | 透明 GPL 价格表 + Financial Account Closure Assistant |
| 15 | Willing | 极简在线遗嘱，MetLife 背书 | 5.8 | "print to pay" 付费门槛设计 |

> 关键发现：**GoodTrust 字面上有一个叫 "dead man switch" 的功能**，与 deadman 项目同名同概念，是最直接的对标/竞品。Empathy 是 AI 智能化程度最高的玩家（B2B2C、$90M 融资、$400M 估值、5M 员工 + 35M 保单持有人覆盖）。

---

## 二、各产品详细分析

### 1. Cake（joincake.com）

**核心功能**：免费终活规划平台，将规划分为 5 区——Funeral / Legacy / Health / Digital / Legal & Financial；可在平台内直接生成 advance directive；提供数千篇教育文章；支持在线 memorial；无安全文件存储/密码管理（明确定位为"规划而非保险箱"）。

**用户交互流程**：注册 → 选择五区中任一进入 → 引导式问答（带示例答案）→ 生成可分享文档 → 邀请家人查看特定分区。Onboarding 极轻，无信用卡。

**AI 智能化程度**：低。主要是引导式表单 + 内容推荐，未见 LLM 接入痕迹。算法主要用于"smart guidance"路径推荐。

**商业模式**：100% 免费 + 广告支持（合作方推荐：保险、殡葬、律师）。机构端通过 healthcare/financial services/insurance 合作分发。由 Harvard 培训的姑息科医生 + MIT 工程师创立，venture-backed，日均 13 万 UV。

**隐私与合规**：标准 SSL（GoDaddy 颁发），Cloudflare CDN，无 vault 故无加密静态存储需求。明确不存储密码、不上传保单文件。

**与 deadman 差异 / 借鉴**：Cake 的"五分区"是绝佳的内容架构范本；其内容营销驱动流量的打法值得 deadman 在中国市场复用（"身后事指南"长尾 SEO）；但 Cake 不做执行、不做 AI、不做家庭协作，正好是 deadman 的能力空缺。

---

### 2. Everplans（everplans.com）

**核心功能**：数字保险箱，9 大分区（Financial / Digital / Legal / Health / Home & Property / Personal / Funeral / Advisors / Emergency）；"Deputies" 受托人系统支持细粒度授权 + 等待期触发；文档过期提醒（驾照、保险续期）；5GB 文件上传。

**用户交互流程**：60 天免费试用（无需信用卡，最长试用期）→ 上传文档/填写账户 → 指定 Deputies 并配置触发条件（立即/等待期/死后）→ Deputies 收到邮件邀请并按授权查看。

**AI 智能化程度**：低-中。"Smart, algorithm-led guidance" 是规则引擎，非 LLM。

**商业模式**：$99.99/年 Premium；B2B 通过 employers / financial advisors 分发（部分合作方价格 < $30/年）；典型 SaaS 续费模式（停付即失访问权，被用户诟病）。

**隐私与合规**：Bank-level security，256-bit AES 加密，TLS in transit + at-rest，两因素认证。注意：密码字段不可存储（仅 VIP 账户在 GoodTrust 可）。

**与 deadman 差异 / 借鉴**：Deputies 的"等待期触发"机制是 deadman 死亡验证模块的直接参考；文档过期提醒是高频活跃钩子（驾照、保单、年检）；但其" filing cabinet"被批评为"冷冰冰的合规工具"，deadman 应在情绪温度上做差异化。

---

### 3. Lantern（lantern.co）

**核心功能**：丧亲前后双轨导航。Pre-planning + Post-loss checklist；**8 类通知信函生成器**（银行死亡通知、雇主通知、人寿保险理赔、订阅取消、执行人通知、债权人通知、订阅取消、雇主通知）；Grief assessment 10 题自评；Letter to your loved one 引导写作；Difficult Date Planner（生日/周年/节日应对计划）；Grief Support Finder 资源匹配。

**用户交互流程**：免费基础工具无需注册 → 选择"我在为身后事规划"或"我失去了亲人"双路径 → 推荐流程清单 → 生成可打印信函 / 计划。

**AI 智能化程度**：低。模板填充式，无 LLM。CEO Liz Eddy 因祖母在护理机构猝然离世、27 岁时面对警察和遗体不知如何处理而创立。

**商业模式**：免费 + $149 一次性解锁高级资源；B2B 通过 hospice / 雇主 EAP 分发。

**隐私与合规**：标准 web 安全，未强调加密细节（因为不存敏感数据）。

**与 deadman 差异 / 借鉴**：**8 类通知信函模板库**是 deadman 流程引擎直接可复用的产物——中国市场可本土化为"户口注销通知 / 社保丧葬费申领 / 公积金提取 / 医保账户注销 / 银行账户解冻 / 房产继承公证 / 信用卡销户 / 互联网账号注销"。Difficult Date Planner 的"未来困难日预测 + 应对预案"是同理心通知的优质 prompt 结构。

---

### 4. Empathy（empathy.com）⭐ AI 标杆

**核心功能**：
- **Personalized Care Plan**：分步骤任务追踪，支持最多 5 名家庭成员协作
- **Account Settlement**：代为关闭云账户、订阅、 memberships（AI 自动化）
- **Dedicated Care Manager**：24/7 真人 Care Manager 专属支持
- **Document Vault**：加密存储死亡证、遗嘱、财务记录
- **Grief Support Library**：文章、音频引导、冥想
- **Benefits Discovery**：识别政府/私营可申领福利
- **AI Obituary Writer**：LLM 生成讣告
- **LifeVault Conversations**（2026 新增）：AI 引导的私人对话空间，预演困难对话
- **Empathy Connect**（2026 新增）：面向财务顾问/保险代理的客户端仪表盘
- **Empathy LifeVault**：与遗产律师共同开发的数字遗产规划工具
- **Empathy Leave Support**：员工请假导航

**用户交互流程**：通过雇主 / 保险公司保单激活（99% B2B2C）→ App 内 onboarding 评估丧亲阶段 → 生成个性化 Care Plan → Care Manager 主动外联 → 家庭成员邀请协作 → AI 自动处理账户关闭 + 真人处理复杂事务 + 情绪支持资源推送。

**AI 智能化程度**：**高，行业最高**。LLM 用于讣告生成、云服务关闭自动化、Care Manager 认知减负（让真人更专注情绪）、LifeVault 对话引导。明确表态"AI 在 grief 中不应取代真人，而是放大真人"。已服务 5M 员工 + 35M 保单持有人。

**商业模式**：B2B2C —— 通过雇主和保险公司分发，对终端用户免费；个人直接购买 $65 一次性。总融资 $90M（Series B $47M by Index Ventures + MassMutual/MetLife/New York Life/Securian/Sumitomo 战略投资），估值约 $400M。**真实效果数据**：家庭平均节省 212 小时 + $3,611，92% 用户反馈感觉更好（行业基准： settle 一个遗产平均需 420 小时 + 15 个月）。

**隐私与合规**：加密文档存储、Care Manager 受专业培训，HIPAA-aware（涉及死亡证等敏感文档）。

**与 deadman 差异 / 借鉴**：Empathy 是 deadman 最完整的对标。
- AI + 真人 Care Manager 双轨 = deadman 多智能体 + 人工介入的最佳范式
- "AI 减负真人而非取代"理念 = 与 deadman 的 AI-RULE 高度契合
- 5 人家庭协作是经验数字
- LifeVault Conversations 的"AI 引导预演困难对话"是 deadman 医疗导航模块可直接借鉴的功能（预演与家属的临终谈话）

---

### 5. Tomorrow（tomorrow.me / tomorrow.app）

**核心功能**：免费遗嘱 + 信托 + 监护人指定 + 最终安排；移动优先；**突出受益人 designation 复核**（提醒用户检查退休账户/人寿保险的 beneficiary 是否仍符合意愿，避免前配偶/已故父母残留）；邀请执行人/监护人短信确认。

**用户交互流程**：下载 App → 三段式 onboarding（Me / Goals / Documents）→ 50 律师审核过的州法合规文档 → 打印 + 公证 + 见证人签字。

**AI 智能化程度**：低。模板填充。

**商业模式**：免费 + 寿险交叉销售（term life insurance 报价）。CEO Dave Hanley 强调"social experience"——让家人从一开始就参与。

**隐私与合规**：加密协议、访问控制。

**与 deadman 差异 / 借鉴**：**Beneficiary designation 复核提醒**是 deadman 时间线/检查清单的关键节点（中国可本土化为"受益人/继承人/监护人复核"，特别是房产、社保、公积金、保险受益人）。"让家人从一开始参与"的 social onboarding 设计也值得借鉴。

---

### 6. Fabric（meetfabric.com，by Gerber Life）

**核心功能**：真正免费的移动端遗嘱（5 分钟完成）+ 监护人指定 + 受益人指定 + 最终安排 + Fabric Vault（云存储）；无 POA、无医疗指令、无信托。

**用户交互流程**：原生 iOS/Android App → 5 分钟问答 → 邮件发送 Will Kit → 打印 + 2 见证人签字 → App 内分享给配偶/监护人。

**AI 智能化程度**：低。但移动 UX 在品类中最佳（评分 9.5/10）。

**商业模式**：免费 + 寿险（Vantis Life 承保的 term life + accidental death）+ 投资账户（儿童）+ 家居保修 + 宠物保险交叉销售。被 Gerber Life 收购后由保险公司买单。

**隐私与合规**：行业标准的加密与存储。

**与 deadman 差异 / 借鉴**：**5 分钟极简 onboarding** 是 deadman 在低意愿用户（年轻/健康/未规划）入口的范本；Fabric Vault 的"will + 账户信息 + 家人共享"三合一值得参考。但功能过窄不适合 deadman 的全流程定位。

---

### 7. Nolo WillMaker & Trust

**核心功能**：50+ 年历史的桌面 + 在线软件；WillMaker Plus 含 7 大文档（Will / Living Trust / Healthcare Directive / Financial POA / Healthcare POA / Final Arrangements / Information for Caregivers and Survivors）+ Pour-Over Will + Trust Certification；household license（一份订阅覆盖全家）。

**用户交互流程**：购买 → 下载桌面软件或在线 portal → 引导式"interview"（每步带详细法律解释）→ 生成州法合规文档 → 打印 + 见证 + 公证。

**AI 智能化程度**：低。律师编辑团队每年更新各州法律。

**商业模式**：$99.99（Starter）/ $149.99（Plus）/ $209（All Access，含 Everplans 一年 Premium）。年付续费制。

**隐私与合规**：TLS + 防火墙 + 系统警报；桌面版数据本地存储（不联网）—— 这是隐私极致派的选择。

**与 deadman 差异 / 借鉴**：**"Information for Caregivers and Survivors" 文档**是 deadman 应该自动生成的产物——把账户信息、文档位置、执行人联系方式、特殊指令汇总成执行人roadmap。**Household license** 全家桶授权模式值得 deadman 在定价上参考。

---

### 8. Trust & Will（trustandwill.com）

**核心功能**：法律文档为核（Will $199 / Trust $499 / Couples $299-599）；州法合规；trust funding instructions；2026 推出 **EstateOS 会员制**：Essentials $49/年 / Pro $299/年 / Concierge $499/年，含 AI Assistant（基础 + 进阶读取上传文档）、Plan Strength Score、Digital Safe、Mobile Notary（上门公证）、Trust-funding 帮助（concierge 协助 5 项资产过户）、Attorney Support（无限 30 分钟通话）、Estate Tax Report、Annual Check-in。

**用户交互流程**：选购套餐 → 引导式问答（每题带 plain-English 解释）→ 生成文档 → 下载或邮寄 → 签字 + 公证 → 上传到 Digital Safe → 通过 AI Assistant 持续更新。

**AI 智能化程度**：中-高。AI Assistant 回答遗产规划问题；Advanced AI 读取上传文档增强回答；Plan Strength Score 是基于规则引擎的"规划完整度评分"。

**商业模式**：一次性文档费 + 年度会员费 + 附加服务（律师 $299/年、公证、trust funding）。已服务 1M+ 用户，Trustpilot 4.5/5。

**隐私与合规**：Bank-level encryption；州法合规是核心卖点；律师网络覆盖 43 州 + DC。

**与 deadman 差异 / 借鉴**：
- **Plan Strength Score** 是 deadman 应该做的"身后事规划完整度评分"（百分比 + 缺失项清单）
- **Mobile Notary 上门公证**是中国本土化的公证协作工作流原型（中国需对接公证处）
- **Trust Funding concierge** 提示 deadman 需要"遗产过户执行协助"功能
- **Annual Check-in** 是 deadman 主动通知伦理的合规表达（年度复核而非频繁打扰）

---

### 9. GoodTrust（mygoodtrust.com）⭐ 含 "Dead Man Switch"

**核心功能**：
- Estate+ Plan $149 + $39/年续费：Revocable Living Trust / Will / Financial Durable POA / Advanced Health Care Directive / **Funeral Directive** / **Pet Directive** / Digital Vault
- **Digital Executor** 服务：VIP 5/10 账户套餐，代为 memorialize Facebook、停 Netflix、提取 PayPal 余额、导出 Google Photos、关闭 LinkedIn；用户签 POA 让 GoodTrust 代表执行
- **"Dead Man Switch"** ⭐：用户设定 1-4 次/月或年的 check-in 频率；系统发邮件；连续 3 次未回复即触发死亡推定，自动执行预设的 Sites & Socials / Documents / Devices / Will & Directives 管理和 Future Messages 发送
- Smart Digital Vault；Family Plan 含 4 个家庭账户；Memorial Markers

**用户交互流程**：注册 → 选择 sites & actions → 上传文档 + 签 POA → 设定 Dead Man Switch check-in 频率 → 持续 check-in → 失联触发自动执行。

**AI 智能化程度**：低-中。Smart Vault 有"智能"组织但未公开 LLM 应用。

**商业模式**：一次性 + 年费；Digital Executor VIP 套餐（5/10 账户）；与 Everis（殡葬+保险+数字纪念伙伴）合作全旅程分发。

**隐私与合规**：256-bit AES 加密、MFA、Digital Vault；用户 POA 是法律执行基础。

**与 deadman 差异 / 借鉴**：**这是 deadman 最直接的同名竞品**。
- Dead Man Switch 的"check-in 频率 + 3 次失联触发"是简单但健壮的死亡推定机制，deadman 应在此基础上做更伦理的版本（多因子验证：邮件 + 短信 + 紧急联系人确认 + 医疗记录可选）
- "Future Messages" 死后定时发送 = deadman 的"身后信件"模块
- Digital Executor 代为账户关闭的 POA 模式 = deadman 的"数字遗产执行"工作流法律基础
- GoodTrust 没有 AI、没有多智能体、没有医疗导航——这正是 deadman 的差异化空间

---

### 10. FreeWill（freewill.com）

**核心功能**：100% 免费遗嘱 + 医疗指令 + 财务 POA + 受益人指定 + 加州 Revocable Living Trust；1.5M+ 遗嘱已生成；Trustpilot 4.9/5；**6 分之 1 用户在遗嘱中加入慈善遗赠**，已为非营利组织承诺 $14.2B+ 遗赠。

**用户交互流程**：填写问卷（20 分钟）→ 生成可打印文档 → 打印 + 签字 + 见证 → 可选：通知 FreeWill 合作的非营利组织。

**AI 智能化程度**：低。规则引导。

**商业模式**：**2400+ 非营利组织付费合作**——非营利为 FreeWill 买单（Featureship 曝光 + lead generation + planned giving 报告）；FreeWill Fellows 网络连接用户与律师（复杂需求转介）；Smart Giving Suite（DAF / 股票 / IRA QCD / 加密货币捐赠）。

**隐私与合规**：明确"绝不售卖个人数据"；bank-level encryption；为非营利提供的 donor 数据受最高安全标准保护。

**与 deadman 差异 / 借鉴**：**慈善遗赠 + 非营利组织分账**是 deadman 在中国市场的潜在商业模式创新——与公募基金会（如希望工程、红十字会）合作，用户在身后事规划中加入遗赠，基金会为 deadman 买单或分账。**Featureship** 区域/全国曝光位是 monetization 路径。

---

### 11. Better Place Forests

**核心功能**：保护性森林中的 Memorial Tree 树葬；9 座受保护森林；4 档树木尺寸（Keepsake $5,900-$6,200 / Legacy $8,500 / Monument $12,900 / Landmark $18,200）；Forest Memorial 仪式（$100 无人参加 / $500-1,500+ 按人数）；Memorial Markers（$500，每树最多 4 个）；Pet Tribute（$300 含与主人合葬）；Arbor Day Foundation 每客户种额外树苗；与 Everis 合作全旅程（火化、保险、数字纪念）。

**用户交互流程**：在线或实地选树 → Forest Guide 协助完成法律文书 + 雕刻 marker → 安排仪式日 → Guide 现场主持灰土混合 → 树下安放 → 永久访问权。

**AI 智能化程度**：无。

**商业模式**：树木购买 + 月供（$122-396/月）+ 仪式费 + marker；保育土地法律多重保护。

**隐私与合规**：标准合同与法律文书（death certificate + spreading affidavit）。

**与 deadman 差异 / 借鉴**：**绿色殡葬的"永久访问权 + 保育绑定"**是 deadman 殡葬导航模块的差异化产品——中国市场可对接生态葬（树葬、海葬、花坛葬）+ 公益林地。但 Better Place Forests 无 AI、无多智能体，deadman 可在"智能匹配林地/海葬资源 + 价格透明 + 全流程代办"上胜出。

---

### 12. eFuneral（efuneral.com）

**核心功能**：在线殡葬规划与购买工具（不自有殡仪馆）；765 家合作殡仪馆；殡仪馆比价 + 评价；预付 secure funding vehicle；abandoned cart lead program（67% 转化为 preneed 销售）；为殡仪馆提供 B2B 销售线索 + 数字销售平台。

**用户交互流程**：在线比价 → 选择 partner provider → 完成在线规划 + 支付（partner 才能在线支付）→ 殡仪馆联系执行。

**AI 智能化程度**：低。规则匹配 + 线索评分。

**商业模式**：B2B SaaS——殡仪馆按月/年订阅 + 免费月度选项；B2C 用户免费。已报价 $1B 殡葬、服务 24k 家庭、lead-to-close 35%、预need 面额高 15%、投保人均龄 68 岁（低于行业平均）。

**隐私与合规**：TLS 1.2 加密；PCI Service Provider Level 1 支付合规；不售卖信息；州法监管的预付资金载体。

**与 deadman 差异 / 借鉴**：**Abandoned cart lead program** 是 deadman 商业化的转化利器——用户规划到一半放弃，deadman 可将线索转介给合作殡仪馆/律师/公证处。**殡仪馆比价 + 评价**是中国市场缺失的透明度产品（中国殡葬价格极度不透明）。

---

### 13. Toast（toastpal.com 实际站点）

**核心功能**：AI 悼文 / 悼词生成器；用户填表（与逝者关系、性格特征、回忆故事、价值观、口头禅）→ AI 生成 500-700 字悼文（3-5 分钟朗读）→ 用户审阅修改 → 完成。已支持 70,000+ 家庭。

**用户交互流程**：访问 → 表单输入 → AI 生成多稿 → 审阅修改 → 打印/分享。

**AI 智能化程度**：高（单点 LLM 应用）。

**商业模式**：freemium + 付费升级。

**隐私与合规**：标准 web 安全。

**与 deadman 差异 / 借鉴**：**AI 悼文生成的 prompt 结构**值得直接借鉴——"姓名 + 关系 + 3-5 个性格特质 + 1-2 个具体回忆 + 价值观/口头禅"是经过验证的输入结构。deadman 应在此基础上扩展为"AI 悼文 + AI 讣告 + AI 答谢词 + AI 墓志铭 + AI 追思会致辞"五合一，并加入多语言（中文古文/现代文）+ 多信仰（佛教/基督教/无神论）+ 多语气（庄重/温暖/幽默）切换。

---

### 14. Afterword（afterword.com）

**核心功能**：**实际上是 B2B 殡仪馆 SaaS**（不是 C 端殡葬 O2O），含 4 大模块：Online Planner（$179/月起，教育视频 + 虚拟选品室 + 自定义套餐）/ Case Management（$325/月，文档 + 无限 eSignature + 支付处理 + 报表）/ Chain of Custody（$149/月，防篡改 GPS 时间戳照片跟踪）/ Task Lists（$99/月，AI 助手按案件类型自动建任务清单）。3-8 周上线，FTC Funeral Rule 合规。同集团 after.com 是 C 端透明定价火化 O2O（直接火化 $995-1,595，比传统殡仪馆省 85-90%），GPL 公开价格表，含 **Financial Account Closure Assistant**（$495）+ Financial Account Discovery Report（$65）+ Fingerprint Preservation + DIY Memorial Video。

**用户交互流程**（after.com C 端）：24/7 电话或在线 → 在线安排 → 接体 + 火化 + 邮寄骨灰 + 在线讣告 + 悲伤支持。

**AI 智能化程度**：B2B 端 Task Lists 含"AI assistant"自动建任务；C 端未见明显 LLM 应用。

**商业模式**：B2B SaaS 月费 + C 端火化服务一次性（$995-1,595 三档）+ 增值（账户关闭助理、指纹保存、纪念视频）。

**隐私与合规**：FTC Funeral Rule 合规；GPL 透明价格；州法合规；Chain of Custody 的防篡改审计追踪。

**与 deadman 差异 / 借鉴**：
- **GPL 透明价格表**是 deadman 殡葬导航的产品标准——中国市场缺失
- **Financial Account Closure Assistant + Discovery Report**是 deadman 数字遗产执行模块的产品化命名范本
- **Chain of Custody 防篡改审计**值得 deadman 在死亡验证 / 文档流转中复用
- **B2B 殡仪馆 SaaS 是 deadman 不应进入的领域**——deadman 应聚焦 C 端多智能体平台，把 B2B 殡仪馆作为合作方生态

---

### 15. Willing（willing.com / willing.co.uk）

**核心功能**：极简在线遗嘱；3 档 $69/$299/$399（美国）或 £29 print-to-pay（英国）；含 will / living will / financial POA；AWS 基础设施 + SSL；30 分钟完成；MetLife 背书的免费版本（basic will）。

**用户交互流程**：注册 → 问卷 → 生成 → **print-to-pay 门槛**（在线填写免费，打印时才付费）→ 签字 + 公证。

**AI 智能化程度**：无。

**商业模式**：freemium + 一次性付费（print-to-pay 是优雅的付费门槛设计）。

**隐私与合规**：AWS ISO 27001 + SAS70 Type II；SSL。但 BBB 评级 F（非 BBB 认证），缺少公开用户评价。

**与 deadman 差异 / 借鉴**：**Print-to-pay 付费门槛**是 deadman 商业化设计的优雅范本——让用户先完成规划、到导出/分享/执行时才付费，转化率更高。其他方面无显著创新，功能过简。

---

## 三、核心结论：deadman 应当借鉴的功能清单（按 P0/P1/P2 分级）

### P0 级（必须做，是 deadman 核心竞争力的补全）

| # | 功能 | 描述 | 为什么借鉴 | 实现复杂度 | 与 deadman 现有架构契合度 | 是否与 AI-RULE 冲突 |
|---|---|---|---|---|---|---|
| P0-1 | **Dead Man Switch 多因子触发** | 邮件 + 短信 + 紧急联系人确认 + 可选医疗记录的死亡推定机制；连续 N 次失联后触发 | GoodTrust 已有同名功能（仅邮件 + 3 次），deadman 必须做到更伦理、更准确 | 中：需要多通道通知 + 死亡验证状态机 | 极高：是 deadman 项目命名的核心机制 | 不冲突，但需 AI-RULE 约束"死亡推定"的不可逆操作必须有真人二次确认 |
| P0-2 | **AI 悼文 / 讣告 / 答谢词 / 墓志铭生成** | Toast + Empathy 已验证的 prompt 结构（关系/特质/回忆/价值观/口头禅）+ 多语言/信仰/语气切换 | Empathy 把 AI 悼文作为入口级功能；Toast 70k 家庭验证 | 低-中：LLM prompt 工程 + 模板 | 高：天然是多智能体中"悼文 agent"的产物 | 不冲突，但要 AI-RULE 约束"不得编造未提供的事实" |
| P0-3 | **结构化丧亲流程清单 + 8 类通知信函本土化** | Lantern 8 类信函本土化为：户口注销/社保丧葬费/公积金/医保/银行/房产继承公证/信用卡销户/互联网账号注销 | Lantern 已验证模板化通知信函的市场需求 | 中：需要本土法律调研 + 模板库 | 高：是 deadman 流程 agent 的产物 | 不冲突 |
| P0-4 | **数字遗产执行 + 账户关闭自动化** | GoodTrust Digital Executor 模式（用户签 POA + VIP 套餐代为关闭 Facebook/Netflix/Google Photos/PayPal/LinkedIn）；中国市场本土化为微信/支付宝/抖音/淘宝/微博账号继承与注销 | GoodTrust + Empathy 已验证可行；中国《互联网用户账号信息管理规定》2025 已生效 | 高：需要法律基础 + 平台 API 对接 + 人工兜底 | 高：是 deadman 数字遗产 agent 的产物 | 部分冲突：AI-RULE 必须约束"账户关闭"是不可逆操作，需真人 + 法定继承人授权 |
| P0-5 | **家庭协作工作空间（最多 5 人）** | Empathy 验证 5 人是丧亲协作的经验上限；任务分配/进度跟踪/文档共享/权限分级 | Empathy 的 5M+ 家庭验证 | 中：权限模型 + 任务状态机 | 极高：多智能体天然多角色 | 不冲突 |
| P0-6 | **加密文档保险箱 + Deputies 等待期触发 + 过期提醒** | Everplans Deputies + 文档过期提醒（驾照/保单/年检）；AES-256 + MFA | Everplans 12 年验证 | 中：加密 + 状态机 + 提醒引擎 | 高：是 deadman 保险箱 agent 的产物 | 不冲突 |

### P1 级（应做，差异化与商业化关键）

| # | 功能 | 描述 | 为什么借鉴 | 实现复杂度 | 与 deadman 现有架构契合度 | 是否与 AI-RULE 冲突 |
|---|---|---|---|---|---|---|
| P1-1 | **Plan Strength Score 规划完整度评分** | Trust & Will 的百分比评分 + 缺失项清单 + 智能建议 | Trust & Will EstateOS 核心功能 | 低：规则引擎 + 评分模型 | 高：是 deadman 评估 agent 的产物 | 不冲突 |
| P1-2 | **保险理赔自动化 + 福利发现** | Empathy Benefits Discovery 识别社保/商保可申领福利 + 自动化理赔流程 | Empathy 节省家庭 212 小时 + $3,611 的核心来源 | 高：需要保险产品库 + 理赔流程自动化 | 高：是 deadman 保险 agent 的产物 | 部分冲突：AI-RULE 必须约束"理赔金额计算"需人工复核 |
| P1-3 | **Mobile Notary / 公证协作工作流** | Trust & Will Mobile Notary 上门公证的中国本土化——对接公证处 + 上门服务 + 视频公证 | Trust & Will Concierge 档核心卖点 | 高：需要公证处网络 + 法律合规 | 中：需要线下服务网络 | 不冲突 |
| P1-4 | **Annual Check-in 年度复核** | Trust & Will Annual Check-in 由遗产规划专家年度复核 | 主动通知伦理的合规表达——年度而非频繁打扰 | 低：定时任务 + 人工/半自动 | 高：是 deadman 主动通知 agent 的产物 | 不冲突，反而是 AI-RULE 友好的低频触达 |
| P1-5 | **GPL 透明价格表 + 殡葬比价** | after.com GPL + eFuneral 比价的中国本土化——殡葬服务价格透明 + 殡仪馆评价 | 中国殡葬价格极度不透明，是用户痛点 | 高：需要殡仪馆网络 + 数据采集 | 中：偏数据驱动，与多智能体契合度一般 | 不冲突 |
| P1-6 | **Information for Caregivers 执行人指南自动生成** | Nolo WillMaker 的执行人 roadmap 文档——账户信息/文档位置/执行人联系方式/特殊指令汇总 | Nolo 50 年验证的刚需 | 低：模板 + 数据汇总 | 高：是 deadman 文档 agent 的产物 | 不冲突 |
| P1-7 | **LifeVault Conversations AI 引导预演困难对话** | Empathy 2026 新功能——AI 引导私人对话空间预演与家属的临终谈话 | Empathy 把 AI 用于情绪准备而非替代情绪 | 中：LLM 对话设计 + 安全边界 | 高：是 deadman 医疗导航 agent 的延伸 | 部分冲突：AI-RULE 必须约束"医疗建议"需医疗专业人员复核 |
| P1-8 | **教育内容库 + Calculator + SEO 长尾** | Cake 数千篇文章驱动流量；FreeWill Planned Giving Report 年度报告；遗产计算器 | 内容获客是美国同类主要获客方式 | 中：内容生产 + SEO + 计算器 | 中：偏内容运营 | 不冲突 |

### P2 级（可选，作为生态扩展或商业化探索）

| # | 功能 | 描述 | 为什么借鉴 | 实现复杂度 | 与 deadman 现有架构契合度 | 是否与 AI-RULE 冲突 |
|---|---|---|---|---|---|---|
| P2-1 | **慈善遗赠 + 非营利分账** | FreeWill 2400+ 非营利分账模式中国本土化——与公募基金会合作 | FreeWill 已筹 $14.2B 遗赠，模式验证 | 中：基金会网络 + 法律合规 | 低：偏商务拓展 | 不冲突 |
| P2-2 | **绿色殡葬 / 纪念树导航** | Better Place Forests Memorial Tree 中国本土化——生态葬/树葬/海葬导航 | 绿色殡葬是趋势；中国有公益林地 | 中：林地/海葬资源对接 | 中：偏资源整合 | 不冲突 |
| P2-3 | **Abandoned Cart Lead 转化** | eFuneral 67% 转化的放弃购物车线索机制——用户规划到一半放弃，转介给合作殡仪馆/律师/公证处 | eFuneral 验证可行 | 低：线索评分 + 转介 | 中：偏商业化 | 不冲突 |
| P2-4 | **Pet Directive 宠物指令** | GoodTrust Pet Directive——宠物监护/继承/安葬 | 中国宠物经济崛起，情感刚需 | 低：模板 | 中 | 不冲突 |
| P2-5 | **Difficult Date Planner 困难日预案** | Lantern 生日/周年/节日应对预案——预测未来困难日 + 应对计划 | 同理心通知的优质 prompt 结构 | 低：日历 + 模板 | 高：是 deadman 主动通知 agent 的产物 | 部分冲突：AI-RULE 必须约束"主动通知频率"避免打扰 |
| P2-6 | **Pet Tribute + Memorial Marker** | Better Place Forests 宠物纪念 + 永久标记 | 情感延伸产品 | 中：物理产品对接 | 低 | 不冲突 |
| P2-7 | **Household License 全家桶授权** | Nolo WillMaker household license——一份订阅覆盖全家 | 定价策略差异化 | 低：定价模型 | 低 | 不冲突 |

---

## 四、差异化机会：deadman 可以做到而竞品做不到的

基于 deadman 的 **多智能体架构 + 规则链 + 主动通知伦理 + 中国本土化** 四大独有能力，以下是竞品做不到的差异化机会：

### 4.1 多智能体协作（竞品全部是单产品/单 Agent）

**所有 15 家竞品都是单产品或单 Agent**：Cake 是规划表单、Empathy 是 Care Manager + AI、Trust & Will 是法律文档、GoodTrust 是数字遗产执行。没有任何一家做到"医疗导航 agent + 遗产规划 agent + 殡葬协调 agent + 情绪陪伴 agent + 数字遗产执行 agent"的协同。

deadman 的差异化机会：
- **跨 Agent 上下文共享**：医疗 agent 识别"临终期"→ 触发遗产规划 agent 提醒完成遗嘱 → 触发殡葬协调 agent 启动预规划 → 触发情绪陪伴 agent 关怀家属。这是任何单产品竞品做不到的。
- **Agent 间规则链编排**：用规则链而非硬编码实现"如果用户已签遗嘱但未指定执行人 → 遗产 agent 推送提醒 + 殡葬 agent 提供执行人候选 + 法律 agent 提供执行人法律义务说明"。
- **统一用户画像**：所有 Agent 共享同一用户画像（医疗状态/资产/家庭/情绪），避免竞品中"遗嘱工具不知道用户医疗状态"的信息孤岛。

### 4.2 规则链（竞品全部是硬编码流程）

**所有竞品都是硬编码流程**：Cake 的五分区、Empathy 的 Care Plan 步骤、Trust & Will 的问答流程都是固化的。

deadman 的差异化机会：
- **可编排规则**：用户可自定义"如果失联 30 天 → 通知紧急联系人 → 60 天 → 通知律师 → 90 天 → 触发死亡推定"。GoodTrust 的 Dead Man Switch 是固定 3 次邮件，deadman 可让用户/律师编排任意规则链。
- **规则可审计**：规则链执行全程上链/审计日志，满足法律合规要求。
- **规则可复用**：律师/公证处/保险可发布"行业模板规则链"，用户一键采用。

### 4.3 主动通知伦理（竞品要么不主动要么打扰）

**竞品的通知要么是被动的（Everplans Deputies 需用户主动配置触发）、要么是高频打扰（各种 marketing email）**。Trust & Will 的 Annual Check-in 是少数伦理的，但频率固定。

deadman 的差异化机会：
- **AI-RULE 约束的主动通知**：deadman 的 AI-RULE 应明确"主动通知频率上限 / 用户情绪状态感知 / 困难日预测 + 应对预案 / 重大事件触发（如医疗状态变化）"。
- **情绪感知通知**：基于用户的悲伤阶段（Kubler-Ross 五阶段 / 双过程模型）调整通知语气与频率——这是 Empathy 的 LifeVault Conversations 已经在做但未与通知系统打通的。
- **伦理的"死亡推定"**：GoodTrust 的"3 次邮件失联即触发"过于简单，deadman 应做到"多因子验证 + 紧急联系人确认 + 医疗记录可选 + 律师介入 + 法定继承人二次确认"的伦理流程，且全程 AI-RULE 约束不可逆操作的二次确认。

### 4.4 中国本土化（15 家竞品 100% 美国市场）

**15 家竞品 100% 美国市场**：遗嘱法律是州法（50 州 + DC）、保险是美国人寿、殡葬是美国殡仪馆、非营利是美国 501(c)(3)。没有任何一家适配中国法律/文化/监管。

deadman 的差异化机会：
- **中国法律适配**：户口注销、社保丧葬费、公积金继承、医保账户注销、房产继承公证（含《民法典》继承编）、遗嘱公证（《公证法》）、遗产税（暂无但需预留）、互联网账号继承（《互联网用户账号信息管理规定》2025）。
- **中国机构对接**：公证处、殡仪馆、社保局、公积金中心、银行、保险公司、互联网平台（微信/支付宝/抖音/淘宝/微博）的 API 或流程对接。
- **中国文化适配**：清明/冬至/忌日的困难日预案、佛教/道教/基督教/无神论的悼文模板、传统丧葬礼仪（守灵/出殡/七七四十九日）的流程清单、家族祠堂/族谱的数字延伸。
- **中国支付适配**：微信支付/支付宝的身后账户管理、公积金/社保的线上申领（中国政务 App 已较成熟，deadman 可对接"国家政务服务平台"）。
- **中国医保导航**：这是 deadman 独有的医疗导航模块——15 家美国竞品全部不涉及医疗决策导航。deadman 可做"重病就医路径 / 临终关怀选择 / 医保报销规则 / 异地就医 / 专家挂号"——这是 Empathy/Cake/Trust & Will 完全做不到的领域。

### 4.5 多 Agent + 主动通知 + 中国本土化的协同差异化

最强差异化是把以上四点协同起来，例如：

- **场景 A**：医疗 agent 识别用户进入临终期 → 规则链触发"提醒完成遗嘱 + 公证 agent 推荐就近公证处 + 殡葬 agent 提供本地殡仪馆 GPL 比价 + 情绪 agent 启动家属 LifeVault Conversations 预演谈话 + 数字遗产 agent 启动账号清单生成"——**没有任何一家竞品能做到这种跨域协同**。
- **场景 B**：用户失联 → Dead Man Switch 多因子触发 → 规则链执行"户口注销通知信函生成 + 社保丧葬费申领 + 公积金提取 + 银行账户解冻 + 房产继承公证启动 + 互联网账号注销 + 微信/支付宝余额继承 + 慈善遗赠执行"——**这是 15 家竞品任何一家都做不到的全流程自动化**（Empathy 最接近但只做账户关闭不做中国本土化）。
- **场景 C**：清明前夕 → 主动通知伦理引擎基于用户情绪状态决定是否推送"困难日预案 + 悼文重温 + 家族协作邀请"——**这是 Lantern 的 Difficult Date Planner + Empathy 的情绪感知的组合，竞品无人做到**。

### 4.6 deadman 不应进入的领域（避免过度扩张）

基于调研，deadman 应明确**不进入**以下领域：
- **B2B 殡仪馆 SaaS**（Afterword 模式）：Afterword 已占据 B2B 殡仪馆 SaaS 头部，deadman 应作为合作方而非竞争者
- **自有殡葬服务**（after.com / Better Place Forests 模式）：重资产、强监管、非 deadman 核心能力
- **桌面法律软件**（Nolo WillMaker 模式）：50 年品牌护城河不可逾越
- **保险承保**（Fabric / Tomorrow 模式）：需金融牌照，与 deadman 定位冲突

deadman 应聚焦"多智能体 C 端平台 + 规则链 + 主动通知"，把 B2B 殡仪馆、保险、公证、基金会作为生态合作方接入。

---

## 五、附录：调研数据来源

本报告所有数据均来自 WebSearch + WebFetch 实时调研，主要来源：
- Cake: revtechventures.com、whenidiefiles.com、freewill.com 三方评测
- Everplans: everplans.com/pricing、eldersafetyhub.com、saasworthy.com、finderslist.com
- Lantern: lantern.co、grieflantern.com（注意：调研中发现 lantern.co 与 grieflantern.com 是两家不同公司，前者是丧亲导航，后者是 AI grief companion）、eterneva.com 采访
- Empathy: builtinnyc.com、tech387.com、listicler.com、empathy.com/blog
- Tomorrow: law-trust.com、finderslist.com、saashub.com
- Fabric: meetfabric.com/wills、law-trust.com、wellkeptwallet.com
- Nolo WillMaker: store.nolo.com、onlinewillmakers.com、retirementliving.com、willmaker.com、law-trust.com
- Trust & Will: trustandwill.com/membership、help.trustandwill.com、legalzoom.com、freewill.com
- GoodTrust: mygoodtrust.com/wgs、mygoodtrust.com/trust、mygoodtrust.com/digital-executor、support.mygoodtrust.com（**含 Dead Man Switch 原文**）、trustworthy.com
- FreeWill: freewill.com、nonprofits.freewill.com
- Better Place Forests: betterplaceforests.org（含 how-it-works-forest-memorials、finding-your-tree、forest-flagstaff、compare-sustainable-options）
- eFuneral: efuneral.com/faq、everybodywiki.com、partner.efuneral.com
- Toast: toastpal.com/eulogy（注意：toast.life 未找到官网，实际产品为 ToastPal）
- Afterword: afterword.com/pricing、after.com/articles/direct-cremation、after.com/articles/cremation-cost、cdn.after.com GPL PDF
- Willing: willing.com、willing.co.uk、shyft.ai/tools/willing、onlinewillmakers.com、finderslist.com

> 报告字数：约 11,000 字（含中英术语）
> 报告路径：/workspace/deadman/docs/competitive-research-round2.md
