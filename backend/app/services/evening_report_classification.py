"""Versioned, exact-label non-industry exclusions for the evening report only.

These declarations classify explicit region/channel/constituent/style labels.
They never infer semantics from loose substrings such as 城市, 数字 or a number.
"""
from __future__ import annotations

import re

RULE_VERSION = 'evening-classification-v1'
_LABELS = {
    'region': frozenset({
        '上海自贸区', '中韩自贸区', '天津自贸区', '广东自贸区', '海南自贸区', '福建自贸区',
        '黑龙江自贸区', '新疆振兴', '京津冀一体化', '横琴新区', '海峡两岸', '粤港澳大湾区',
        '长三角一体化', '雄安新区', '自由贸易港', '共同富裕示范区', '西部大开发',
    }),
    'trading_channel': frozenset({'沪股通', '深股通', '融资融券'}),
    'index_constituent': frozenset({
        '沪深300', '沪深300成份股', '沪深300成份', '上证50', '上证180',
        '中证500', '中证1000', '中证2000', '深证100', '深证100R',
        'MSCI概念', '富时罗素概念', '标普道琼斯A股',
    }),
    'style': frozenset({
        '高股息精选', '新股与次新股', '注册制次新股', '科创次新股', 'ST板块', '摘帽',
        '中字头股票', '证金持股', '国家大基金持股', '专精特新', '国企改革',
        '央企国企改革', '上海国企改革', '深圳国企改革',
    }),
}
_REASONS = {'region': '地域或区域政策标签，不作为产业板块',
            'trading_channel': '交易通道或融资资格标签，不作为产业板块',
            'index_constituent': '明确指数成分标签，不作为产业板块',
            'style': '股票风格、所有权或状态标签，不作为产业板块'}


def exclusion(label: str) -> tuple[str, str] | None:
    for category, labels in _LABELS.items():
        if label in labels:
            return category, _REASONS[category]
    if re.fullmatch(r'20\d{2}(一季报|中报|三季报|年报)预增', label):
        return 'style', _REASONS['style']
    return None
