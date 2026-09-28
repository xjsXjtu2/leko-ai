// 在淘宝商品页用「搜本店」搜索店铺内商品
// 用法: node shop_search.mjs <商品页URL> <关键词> <输出名>
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire('/Users/xjs/.workbuddy/binaries/node/workspace/');
const { chromium } = require('playwright-core');

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222';
const [itemUrl, keyword, outName = 'shop-search'] = process.argv.slice(2);
const OUT_DIR = '/Users/xjs/github/leko-ai/.workbuddy/tmp';

const browser = await chromium.connectOverCDP(CDP);
const ctx = browser.contexts()[0];
let page = ctx.pages().find((p) => !p.isClosed() && p.url().startsWith('http')) || (await ctx.newPage());

await page.goto(itemUrl, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
await page.waitForTimeout(4000);

const inputSel = 'input[name="keyword"], input#q, input[placeholder*="搜本店"], input[placeholder*="搜索"]';
const input = await page.$(inputSel);
if (!input) {
  console.log('NO_SEARCH_INPUT');
} else {
  await input.click();
  await input.fill('');
  await input.type(keyword, { delay: 40 });
  await page.keyboard.press('Enter');
  await page.waitForTimeout(6000);
}

await page.waitForTimeout(1500);
const info = await page.evaluate(() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="item.htm"], a[href*="detail.tmall.com"]').forEach((a) => {
    const href = a.href.split('&spm=')[0];
    const m = href.match(/[?&]id=(\d+)/);
    if (!m) return;
    const card = a.closest('div[class*="Card"], li[class*="item"], div[class*="item"]') || a.parentElement;
    const text = (card?.innerText || a.innerText || '').replace(/\n+/g, ' | ').slice(0, 200);
    const key = m[1] + '|' + text.slice(0, 30);
    if (seen.has(key) || !text) return;
    seen.add(key);
    out.push({ id: m[1], text, href });
  });
  return { url: location.href, title: document.title, body: document.body.innerText.slice(0, 4000), items: out };
});

const shot = path.join(OUT_DIR, `${outName}.png`);
await page.screenshot({ path: shot, fullPage: false });
fs.writeFileSync(path.join(OUT_DIR, `${outName}.txt`), `URL: ${info.url}\n\n${info.body}\n\n=== ITEMS ===\n` + info.items.map((i, n) => `[${n + 1}] ${i.text}\n    ${i.href}`).join('\n'), 'utf8');

console.log('FINAL_URL:', info.url);
console.log('SHOT:', shot);
console.log('=== ITEMS:', info.items.length, '===');
info.items.forEach((i, n) => console.log(`[${n + 1}] ${i.text}\n    ${i.href}`));

await browser.close();
