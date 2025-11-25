from datetime import datetime
from pathlib import Path

import jinja2
from playwright.async_api import async_playwright

from astrbot.api import logger
from astrbot.api.star import StarTools

# 获取插件的数据目录，用于存放缓存图片
DATA_DIR = StarTools.get_data_dir("astrbot_plugin_ip_proxy")
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# HTML 模板
STATUS_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>代理状态</title>
<style>
    /* ... CSS 样式保持不变 ... */
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        background-color: #f0f2f5;
        padding: 20px;
        width: 600px;
        margin: 0;
        box-sizing: border-box;
    }
    .container {
        background-color: white;
        border-radius: 12px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.08);
        padding: 25px;
    }
    .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding-bottom: 15px;
        border-bottom: 1px solid #e8e8e8;
        margin-bottom: 20px;
    }
    .header h1 {
        font-size: 24px;
        font-weight: 700;
        color: #333;
        margin: 0;
    }
    .status-badge {
        padding: 6px 12px;
        border-radius: 16px;
        font-weight: 600;
        font-size: 16px;
    }
    .status-running {
        background-color: #e6f7ff;
        color: #1890ff;
        border: 1px solid #91d5ff;
    }
    .status-stopped {
        background-color: #fff1f0;
        color: #f5222d;
        border: 1px solid #ffa39e;
    }
    .info-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 15px;
    }
    .info-card {
        background-color: #fafafa;
        border: 1px solid #f0f0f0;
        border-radius: 8px;
        padding: 15px;
        text-align: center;
    }
    .info-card h3 {
        margin: 0 0 10px 0;
        color: #6c757d;
        font-size: 15px;
        font-weight: 500;
    }
    .info-card .value {
        font-size: 22px;
        font-weight: 700;
        word-wrap: break-word;
    }
    .value.ip { font-size: 18px; }
    .value.small { font-size: 16px; }
    .progress-bar-container {
        background-color: #e9ecef;
        border-radius: 5px;
        height: 10px;
        margin-top: 5px;
        overflow: hidden;
    }
    .progress-bar {
        background-color: #1890ff;
        height: 100%;
        width: {{ traffic_percentage }}%;
        transition: width 0.3s ease-in-out;
    }
    .text-success { color: #52c41a; }
    .text-primary { color: #1890ff; }
    .footer {
        text-align: center;
        margin-top: 25px;
        font-size: 12px;
        color: #aaa;
    }
</style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>代理状态</h1>
            <span class="status-badge {{ 'status-running' if is_running else 'status-stopped' }}">{{ '运行中' if is_running else '已停止' }}</span>
        </div>

        <div class="info-grid">
            <div class="info-card" style="grid-column: 1 / -1;">
                <h3>监听地址</h3>
                <p class="value">{{ listen_address }}</p>
            </div>
            <div class="info-card" style="grid-column: 1 / -1;">
                <h3>当前代理 IP</h3>
                <p class="value ip">{{ current_ip }}</p>
            </div>
            <div class="info-card" style="grid-column: 1 / -1;">
                <h3>流量使用情况</h3>
                <p class="value small">{{ used_traffic }} / {{ total_limit }}</p>
                {% if has_limit %}
                <div class="progress-bar-container">
                    <div class="progress-bar"></div>
                </div>
                {% endif %}
            </div>
            <div class="info-card">
                <h3>剩余流量</h3>
                <p class="value text-primary">{{ remaining_traffic }}</p>
            </div>
            <div class="info-card">
                <h3>预计可用</h3>
                <p class="value text-primary">{{ estimated_days }}</p>
            </div>
            <div class="info-card">
                <h3>今日流量</h3>
                <p class="value">{{ today_traffic }}</p>
            </div>
             <div class="info-card">
                <h3>每日平均</h3>
                <p class="value">{{ daily_avg_traffic }}</p>
            </div>
            <div class="info-card">
                <h3>今日请求成功率</h3>
                <p class="value text-success">{{ success_rate }}</p>
            </div>
            <div class="info-card">
                <h3>今日 IP 使用</h3>
                <p class="value">{{ today_ips }} 个</p>
            </div>
        </div>
        <p class="footer">Generated on: {{ update_time }}</p>
    </div>
</body>
</html>
"""

async def render_status_card(data: dict) -> Path:
    """
    使用Playwright将HTML模板渲染成图片。
    :param data: 包含要在模板中显示的数据的字典。
    :return: 生成的图片的路径。
    """
    template = jinja2.Template(STATUS_HTML_TEMPLATE)
    final_html = template.render(data)

    output_path = CACHE_DIR / f"proxy_status_{int(datetime.now().timestamp())}.png"

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_viewport_size({"width": 640, "height": 780})
        await page.set_content(final_html)

        container_element = await page.query_selector(".container")
        if container_element:
            await container_element.screenshot(path=str(output_path))
        else:
            await page.screenshot(path=str(output_path))

        await browser.close()
        logger.info(f"成功生成代理状态卡片: {output_path}")

    return output_path
