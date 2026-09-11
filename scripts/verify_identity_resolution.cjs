/** Read-only verification of a pump candidate against current published evidence. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const base = process.env.BROWSER_QA_URL || 'http://127.0.0.1:8002';
const origin = new URL(base).origin;
const output = path.resolve(process.env.BROWSER_QA_OUTPUT || '.local/browser-qa/identity-resolution');
const candidateRecordId = process.env.IDENTITY_QA_RECORD_ID;
const targetEntityId = process.env.IDENTITY_QA_TARGET_ID;
assert.ok(candidateRecordId && targetEntityId, 'Set IDENTITY_QA_RECORD_ID and IDENTITY_QA_TARGET_ID for the current pump workspace');
const report = { status: 'running', base, screenshots: [], viewports: [], pageErrors: [], failedResponses: [], blockedRequests: [] };
const readPosts = new Set([
  '/playground/session', '/v1/knowledge/graph:query', '/v1/knowledge/graph:evidence',
  '/v1/knowledge/sources:query', '/v1/knowledge/sources:read',
  '/v1/knowledge/review-evidence', '/v1/knowledge/publications:compare', '/v1/knowledge/quality/reviews:query',
]);
const readGets = /^\/v1\/(?:ontologies|knowledge\/(?:construction-jobs|review-queue|publication-candidates|publications|entity-resolution|review-assessments|records|property-assignment|publication-inventory|documents|quality))(?:\/|$)/;

(async () => {
  assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(base).hostname));
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  report.browser = browser.version();
  report.playwright = require('playwright/package.json').version;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const bootstrapResponse = await context.request.get(base + '/playground/bootstrap');
    assert.ok(bootstrapResponse.ok());
    const bootstrap = await bootstrapResponse.json();
    assert.equal(bootstrap.data_scope, 'pump-only');
    report.dataScope = bootstrap.data_scope;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      const code = url.pathname === '/industrial' || url.pathname.startsWith('/industrial/assets/');
      const allowedRead = ['GET', 'HEAD'].includes(request.method()) && (
        code || url.pathname === '/playground/bootstrap' || url.pathname === '/favicon.ico' ||
        /^\/playground\/(?:assets\/)?industrial-demo-v1\//.test(url.pathname) || readGets.test(url.pathname)
      );
      if (url.origin !== origin || !(allowedRead || request.method() === 'POST' && readPosts.has(url.pathname))) {
        report.blockedRequests.push({ method: request.method(), path: url.pathname });
        return route.abort('blockedbyclient');
      }
      await route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror', error => report.pageErrors.push(error.message));
    page.on('response', response => {
      if (response.status() >= 400) report.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
    });
    const initialGraph = page.waitForResponse(r => new URL(r.url()).pathname === '/v1/knowledge/graph:query');
    await page.goto(base + '/industrial');
    assert.ok((await initialGraph).ok(), 'real workbench graph read must succeed');
    await page.locator('.graph-toolbar').waitFor({ state: 'visible' });
    await page.locator('[data-panel="build"]').click();
    await page.locator('[data-step-target="step-review"]').click();
    const row = page.locator(`[data-review-record="${candidateRecordId}"]`);
    await row.waitFor({state:'visible'});
    const details = row.locator('[data-resolution-details]');
    if (await details.getAttribute('open') === null) await details.locator(':scope > summary').click();
    const query = row.locator('[data-resolution-query]');
    await query.waitFor({state:'visible'});
    await query.fill('循环水泵');
    const searched = page.waitForResponse(r => new URL(r.url()).pathname.includes('/entity-resolution/') && new URL(r.url()).searchParams.get('query') === '循环水泵');
    await row.locator('[data-resolution-search]').click();
    const response = await searched;
    assert.ok(response.ok());
    const payload = await response.json();
    const data = payload.data || payload;
    assert.ok(data.suggestions.some(s => s.target?.entity_id === targetEntityId));
    assert.ok(data.review_targets.some(t => t.selectable && t.entity.entity_id === targetEntityId));
    report.automaticAndManualMatch = true;
    await row.locator('[data-resolution-manual="0"]').waitFor({state:'visible'});
    assert.ok(await row.locator('[data-resolution-manual="0"]').isEnabled());
    for (const viewport of [{width:1280,height:800},{width:1440,height:1000},{width:1920,height:1080}]) {
      await page.setViewportSize(viewport);
      const manual = row.locator('[data-resolution-query]').locator('..').locator('..');
      await manual.scrollIntoViewIfNeeded();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const filename = `matching-${viewport.width}.png`;
      await page.screenshot({path:path.join(output,filename)});
      report.screenshots.push(filename);
      report.viewports.push(viewport);
    }
    const evidence = row.locator('details').filter({has:page.getByText('查看目标实体原文',{exact:true})}).last();
    const read = page.waitForResponse(r => new URL(r.url()).pathname === '/v1/knowledge/review-evidence');
    await evidence.locator(':scope > summary').click();
    assert.ok((await read).ok());
    await evidence.getByText('展开前后文',{exact:true}).waitFor();
    assert.ok((await evidence.textContent()).includes('BC-P-101'));
    await evidence.scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'target-evidence.png')});
    report.screenshots.push('target-evidence.png');
    const full = page.waitForResponse(r => new URL(r.url()).pathname === '/v1/knowledge/review-evidence');
    await evidence.getByText('查看完整文档',{exact:true}).click();
    assert.ok((await full).ok());
    report.publishedEvidenceVerified = true;
    await page.evaluate(() => document.documentElement.requestFullscreen());
    await evidence.scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'fullscreen-evidence.png')});
    report.screenshots.push('fullscreen-evidence.png');
    await page.evaluate(() => document.exitFullscreen());
    report.pageTitle = await page.title();
    report.knowledgeWrites = 0;
    assert.deepEqual(report.pageErrors, []);
    assert.deepEqual(report.failedResponses, []);
    assert.deepEqual(report.blockedRequests, []);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.error = error.message;
    throw error;
  } finally {
    await browser.close();
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ status: report.status, browser: report.browser, output, screenshots: report.screenshots }));
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
