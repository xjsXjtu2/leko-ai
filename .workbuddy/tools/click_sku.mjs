// 点击指定文字的 12V SKU，读取加补后价格
import { createRequire } from 'node:module';
const require = createRequire('/Users/xjs/.workbuddy/binaries/node/workspace/');
const { chromium } = require('playwright-core');

const skuText = process.argv[2] || '6000';

const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
const ctx = browser.contexts()[0];
const page = ctx.pages().find((p) => p.url().includes('item.taobao.com'));
if (!page) { console.error('没有商品页'); process.exit(1); }

const clicked = await page.evaluate((kw) => {
  const spans = [...document.querySelectorAll('span[class*="valueItemText"]')].filter(
    (el) => el.textContent.trim().startsWith(kw)
  );
  if (!spans.length) return 'NO_MATCH';
  const item = spans[0].closest('div[class*="valueItem"]') || spans[0];
  item.click();
  return 'CLICKED:' + spans[0].textContent.trim();
}, skuText);
console.log(clicked);

await page.waitForTimeout(2500);
const price = await page.evaluate(() => {
  const t = document.body.innerText;
  const m = t.match(/平台加[补撇]?后[\s\S]{0,30}?[¥￥][\s\S]{0,15}?[\d.]+/);
  return m ? m[0].replace(/\s+/g, ' ') : 'PRICE_NOT_FOUND';
});
console.log('价格:', price);
process.exit(0);
