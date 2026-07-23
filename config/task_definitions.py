from __future__ import annotations

# 来源：n8n 的 03～06 氛围摆放、07～12 使用场景、13 尺寸/白底承接链路。
# 图 08 的 n8n 分支未连接，但 GPT拟真14→15 仍保留为可选任务配置。
# 图 13 作为第一个功能图槽位，默认选择尺寸图模板，也可在新建项目时改选其他功能图模板。
TASK_DEFINITIONS = [
    {"id": "03", "name": "氛围感摆放图 1", "prompt_group": "atmosphere", "logic": "氛围摆放", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第1张氛围摆放图：按照{{摆放展示要求}}，将{{产品数量}}个{{产品外观参考图}}中的产品安排为清晰、自然的摆放方式；产品居中、完整、占据主要画面，不出现人体，不添加无关文字。"},
    {"id": "04", "name": "氛围感摆放图 2", "prompt_group": "atmosphere", "logic": "氛围摆放", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第2张氛围摆放图：展示{{产品数量}}个{{产品外观参考图}}中的产品，使用少量与产品主题同源的道具，构成可爱、干净、拟真的电商主视觉，背景不得抢主体。"},
    {"id": "05", "name": "氛围感摆放图 3", "prompt_group": "atmosphere", "logic": "氛围摆放", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第3张氛围摆放图：展示{{产品数量}}个{{产品外观参考图}}中的产品，加入更丰富但克制的主题道具和设计元素，保持产品完整清晰，画面层次丰富但不杂乱。"},
    {"id": "06", "name": "氛围感摆放图 4", "prompt_group": "atmosphere", "logic": "氛围摆放", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第4张氛围摆放图：展示{{产品数量}}个{{产品外观参考图}}中的产品，使用与产品主题相符的食物或生活道具营造拟真氛围，但产品本身绝不能变成真实食物，且仍需完整突出展示。"},
    {"id": "07", "name": "亲子送礼图 1", "prompt_group": "scene", "logic": "使用场景", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第1张使用场景图：室内第三人称视角，妈妈弯腰把{{产品外观参考图}}中的产品递给美国小学阶段孩子；产品真实大小严格参考{{手托比例参考图}}，孩子伸手接取并用肢体表现惊喜，不出现礼盒，完整面部不得入镜。"},
    {"id": "08", "name": "亲子送礼图 2", "prompt_group": "scene", "logic": "使用场景", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第2张使用场景图：以{{产品外观参考图}}锁定产品外观，以{{手托比例参考图}}锁定手与产品的真实大小比例，表现温馨、自然、真实的亲子送礼互动；比例参考不得覆盖产品外观。"},
    {"id": "09", "name": "职场使用场景图", "prompt_group": "scene", "logic": "使用场景", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第3张使用场景图：美国职场女性坐在电脑前自然使用{{产品外观参考图}}中的产品，产品真实大小严格参考{{手托比例参考图}}；握持处有轻微真实凹陷，呈现放松解压氛围，只拍肩部以下或局部下半脸。"},
    {"id": "10", "name": "生日派对使用场景图", "prompt_group": "scene", "logic": "使用场景", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第4张使用场景图：生日蛋糕旁，一个美国孩子惊喜地拿着{{产品外观参考图}}中的产品，产品真实大小严格参考{{手托比例参考图}}；另外两个孩子从两侧围拢观察，其中一人准备伸手触碰，完整面部不得入镜。"},
    {"id": "11", "name": "自定义使用场景图", "prompt_group": "scene", "logic": "使用场景", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第5张使用场景图：把{{自定义使用场景}}扩写为真实、自然、可执行的电商摄影画面，主体必须是{{产品外观参考图}}中的产品，产品真实大小严格参考{{手托比例参考图}}；用户要求与场景图全局约束冲突时以后者优先。"},
    {"id": "12", "name": "课堂教具使用场景图", "prompt_group": "scene", "logic": "使用场景", "reference_fields": ["stt", "cpt"], "required": True, "brief": "独立生成第6张使用场景图：美国小学教室，把{{产品外观参考图}}中的产品与书本文具放在讲台，产品真实大小严格参考{{手托比例参考图}}；老师只拍肩部以下并用手指向产品，前排学生以背影或侧后方轮廓举手回答。"},
    {"id": "13", "name": "功能图 1", "prompt_group": "size", "logic": "功能图", "reference_fields": ["stt", "cpt"], "required": True, "brief": "使用新建项目时选择的功能图完整模板作为唯一生成要求，由 GPT-5.5 结合参考图重写为最终中文描述词；不叠加摆放图或场景图约束。"},
]

TASK_DEFINITION_MAP = {item["id"]: item for item in TASK_DEFINITIONS}
