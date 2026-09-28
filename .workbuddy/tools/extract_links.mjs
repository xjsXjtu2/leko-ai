// 从当前已打开的搜索页里提取商品卡片：标题 / 链接 / 价格 / 店铺
import { createRequire } from 'node:module';
const require = createRequire('/Users/xjs/.workbuddy/binaries/node/workspace/');
const { chromium } = require('playwright-core');

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222';
const browser = await chromium.connectOverCDP(CDP);
const ctx = browser.contexts()[0];
const page = ctx.pages().find((p) => !p.isClosed() && p.url().startsWith('http'));
if (!page) {
  console.error('没有可用的页面');
  process.exit(1);
}
console.log('PAGE:', page.url());

const items = await page.evaluate(() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="item.htm"], a[href*="detail.tmall.com"], a[href*="item.taobao.com"]').forEach((a) => {
    const href = a.href.split('&spm=')[0];
    const idMatch = href.match(/[?&]id=(\d+)/);
    if (!idMatch) return;
    const card = a.closest('div[class*="Card"], div[class*="doubleCard"], div[class*="item"]') || a.parentElement;
    const text = (card?.innerText || '').replace(/\n+/g, ' | ').slice(0, 220);
    const key = idMatch[1] + '|' + text.slice(0, 40);
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ id: idMatch[1], href, text });
  });
  return out;
});

console.log('COUNT:', items.length);
items.forEach((it, i) => {
  console.log(`\n[${i + 1}] id=${it.id}`);
  console.log('  ', it.text);
  console.log('  ', it.href);
});

await browser.close();
