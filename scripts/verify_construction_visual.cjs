/** Real Chromium checks of the pump-only construction workbench. No knowledge writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const base = process.env.CONSTRUCTION_QA_URL || 'http://127.0.0.1:8002';
const origin = new URL(base).origin;
const output = path.resolve(process.env.CONSTRUCTION_QA_OUTPUT || 'docs/validation/construction-tabs-visual-2026-09-10');
const pumpDocument = path.resolve('src/graphrag_prod/playground/static/industrial-demo-v1/maintenance_report.txt');
const steps = [
  ['source-upload-slot', '01 上传资料', 'upload-card'],
  ['step-review', '02 复核候选', 'step-review'],
  ['step-publication', '03 发布知识', 'step-publication'],
];
const results = {
  base, status: 'running', dataScope: null, observations: [], screenshots: [],
  pageErrors: [], consoleErrors: [], failedResponses: [], blockedRequests: [], requests: [],
  limitations: ['No upload, extraction, review decision, ontology save, import, preview or publication is submitted.'],
};

(async () => {
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local pump-only server');
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.on('pageerror', error => results.pageErrors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error') results.consoleErrors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 400) results.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
  });
  try {
    // Check the actual running service before loading any application or corpus data.
    const bootstrap = await context.request.get(base + '/playground/bootstrap');
    assert.equal(bootstrap.ok(), true, 'bootstrap must succeed');
    results.dataScope = (await bootstrap.json()).data_scope;
    assert.equal(results.dataScope, 'pump-only');
    const readOnlyPosts = new Set(['/playground/session', '/v1/knowledge/graph:query']);
    const readOnlyApi = /^\/v1\/(?:ontologies|knowledge\/(?:construction-jobs|review-queue|publication-candidates|publications|entity-resolution|review-assessments))(?:\/|$)/;
    await context.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      const method = request.method();
      const sameOrigin = url.origin === origin;
      const codeAsset = url.pathname === '/industrial' || url.pathname.startsWith('/industrial/assets/');
      const pumpAsset = /^\/playground\/(?:assets\/)?industrial-demo-v1\//.test(url.pathname);
      const safeRead = ['GET', 'HEAD'].includes(method) && (
        codeAsset || pumpAsset || url.pathname === '/favicon.ico' ||
        url.pathname === '/playground/bootstrap' || readOnlyApi.test(url.pathname)
      );
      const allowed = sameOrigin && (safeRead || (method === 'POST' && readOnlyPosts.has(url.pathname)));
      const event = { method, path: url.pathname };
      results.requests.push(event);
      if (!allowed) {
        results.blockedRequests.push(event);
        await route.abort('blockedbyclient');
        return;
      }
      await route.continue();
    });

    async function settle() {
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    }
    async function toTop() {
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
      await settle();
    }
    async function shot(name) {
      const filename = name + '.png';
      await page.screenshot({ path: path.join(output, filename) });
      results.screenshots.push(filename);
    }
    async function metrics(name) {
      const measurement = await page.evaluate(() => {
        const rect = selector => {
          const node = document.querySelector(selector);
          if (!node || !node.getClientRects().length) return null;
          const { x, y, width, height } = node.getBoundingClientRect();
          return { x, y, width, height, bottom: y + height, right: x + width };
        };
        return {
          width: innerWidth, height: innerHeight, scrollY, fullscreen: !!document.fullscreenElement,
          pageWidth: document.documentElement.scrollWidth,
          ontology: rect('#expert-foundation'), instance: rect('#instance-workspace'), tabs: rect('.build-view-tabs'),
          upload: rect('#upload-card'), review: rect('#step-review'), publication: rect('#step-publication'),
          sourceType: rect('#document-knowledge-scope'),
          fileDrop: rect('.file-drop'),
          fileInput: rect('#document-file'),
          submit: rect('#construct-button'),
          submitBar: rect('.upload-submit-bar'), accessNote: rect('#document-access-note'),
          steps: [...document.querySelectorAll('#construction-flow-steps [data-step-target]')].map(node => ({
            target: node.dataset.stepTarget, label: node.textContent.trim(), current: node.getAttribute('aria-current'),
            ...rect(`[data-step-target="${node.dataset.stepTarget}"]`),
          })),
        };
      });
      results.observations.push({ name, ...measurement });
      assert.ok(measurement.pageWidth <= measurement.width, name + ': horizontal page overflow');
      if (measurement.ontology) {
        assert.equal(measurement.instance, null, 'ontology occupies a separate tab');
        return measurement;
      }
      assert.ok(measurement.instance.width <= 1160, 'working surface has a bounded desktop width');
      assert.equal(measurement.steps.length, 3, name + ': exactly three construction steps');
      assert.ok(measurement.steps.every(step => Math.abs(step.y - measurement.steps[0].y) <= 1), name + ': steps must remain in one horizontal row');
      assert.ok(measurement.steps.every(step => step.x >= 0 && step.right <= measurement.width), name + ': all step controls must fit');
      return measurement;
    }
    async function assertStep(target) {
      for (const [id, , panel] of steps) {
        assert.equal(await page.locator('#' + panel).isVisible(), id === target, panel + ': selected step visibility');
        assert.equal(await page.locator(`[data-step-target="${id}"]`).getAttribute('aria-current'), id === target ? 'step' : 'false');
      }
    }
    async function chooseStep(target) {
      await page.locator(`[data-step-target="${target}"]`).click();
      await assertStep(target);
      await settle();
    }
    async function draft() {
      return page.evaluate(() => ({
        title: document.querySelector('#document-title').value,
        uri: document.querySelector('#document-uri').value,
        source: document.querySelector('#document-source').value,
        language: document.querySelector('#document-language').value,
        ontology: document.querySelector('#document-tbox').value,
        mode: document.querySelector('#document-extraction-mode').value,
        file: [...document.querySelector('#document-file').files].map(file => ({ name: file.name, size: file.size })),
        accessGroups: document.querySelector('#document-access-group')?.value,
      }));
    }
    async function assertScope(scope, expectedDraft) {
      assert.equal(await page.locator('#document-knowledge-scope').inputValue(), scope);
      assert.equal(await page.locator('#upload-title').textContent(), '上传资料');
      assert.equal(await page.locator('#publication-title').textContent(), '发布知识');
      assert.deepEqual(await draft(), expectedDraft, 'source type and navigation must preserve the upload draft');
    }

    for (const [width, height, name] of [[1280, 800, 'laptop'], [1440, 1000, 'desktop'], [1920, 1080, 'wide-desktop']]) {
      await page.setViewportSize({ width, height });
      await page.goto(base + '/industrial');
      await page.waitForFunction(() => document.querySelector('#node-count')?.textContent !== '—' && document.querySelector('#graph-placeholder')?.hidden);
      await page.locator('[data-panel="build"]').click();
      await page.waitForFunction(() => {
        const status = document.querySelector('#foundation-status');
        return status && !status.textContent.includes('正在读取') && document.querySelector('#document-tbox')?.value;
      });
      assert.deepEqual(await page.locator('[data-build-view]').allTextContents(), ['本体构建', '实例构建']);
      assert.equal(await page.locator('#panel-build > .page-heading').count(), 1, 'one shared page introduction');
      assert.equal(await page.locator('#panel-build .page-heading h1').textContent(), '把资料变成有据可查的知识。');
      assert.equal(await page.locator('#panel-build .page-heading .eyebrow').textContent(), 'BUILD WITH EVIDENCE');
      assert.equal(await page.locator('#build-tab-instances').getAttribute('aria-selected'), 'true');
      assert.equal(await page.locator('#instance-workspace .instance-step-nav').count(), 1);
      assert.equal(await page.locator('[data-flow-choice]').count(), 0, 'parallel business/authoritative flow tabs are removed');
      assert.equal(await page.locator('#step-review #expert-abox').count(), 1, 'advanced instance JSON belongs to instance review');
      assert.equal(await page.locator('#expert-foundation #expert-abox').count(), 0);
      assert.deepEqual(await page.locator('#construction-flow-steps [data-step-target]').evaluateAll(nodes => nodes.map(node => [node.dataset.stepTarget, node.textContent.trim()])), steps.map(([id, label]) => [id, label]));
      assert.deepEqual(await page.locator('#document-knowledge-scope option').evaluateAll(nodes => nodes.map(node => node.value).sort()), ['AUTHORITATIVE', 'BUSINESS']);
      assert.equal(await page.locator('#expert-foundation').isVisible(), false, 'active ontology defaults to the instance tab');
      await assertStep('source-upload-slot');
      await toTop();
      const firstScreen = await metrics(name + '-upload');
      assert.ok(firstScreen.sourceType && firstScreen.sourceType.bottom <= height, name + ': source type selector must be discoverable on the first screen');
      assert.ok(firstScreen.fileInput && firstScreen.fileInput.y >= 0 && firstScreen.fileInput.bottom <= height, name + ': complete file selection control must fit on the first screen');
      assert.ok(firstScreen.submit && firstScreen.submit.y >= 0 && firstScreen.submit.bottom <= height, name + ': main construction action must fit on the first screen');
      assert.ok(firstScreen.accessNote.bottom <= firstScreen.submitBar.y - 8, name + ': upload action must not cover access guidance');
      assert.equal(await page.locator('#construct-button').textContent(), '上传并构建');
      await shot(name + '-01-upload');
      if (name === 'laptop') {
        await page.locator('#construct-button').click();
        await page.waitForFunction(() => {
          const output = document.querySelector('#construction-output');
          const bar = document.querySelector('.upload-submit-bar');
          return !output.hidden && output.getBoundingClientRect().top >= bar.getBoundingClientRect().bottom;
        });
        assert.equal(await page.locator('#construction-output').textContent(), '请选择一个文档。');
        assert.ok(!results.requests.some(request => request.path === '/v1/knowledge:construct'), 'empty file validation must not submit a construction request');
        await shot(name + '-06-upload-validation');
        await page.goto(base + '/industrial');
        await page.waitForFunction(() => document.querySelector('#node-count')?.textContent !== '—' && document.querySelector('#graph-placeholder')?.hidden);
        await page.locator('[data-panel="build"]').click();
        await page.waitForFunction(() => document.querySelector('#document-tbox')?.value);
        await assertStep('source-upload-slot');
      }

      await page.locator('#document-title').fill('循环水泵维护资料 · 仅界面验收草稿');
      await page.locator('.upload-options').filter({ has: page.locator('#document-uri') }).locator(':scope > summary').click();
      await page.locator('#document-uri').fill('urn:local:construction-visual:pump-draft');
      await page.locator('#document-source').fill('循环水泵测试包');
      await page.locator('.upload-options').filter({ has: page.locator('#document-uri') }).locator(':scope > summary').click();
      await page.locator('#document-file').setInputFiles(pumpDocument);
      const expectedDraft = await draft();
      await page.locator('#document-knowledge-scope').selectOption('AUTHORITATIVE');
      await assertScope('AUTHORITATIVE', expectedDraft);
      await toTop();
      await metrics(name + '-authoritative');
      await shot(name + '-02-authoritative');

      await chooseStep('step-review');
      await assertScope('AUTHORITATIVE', expectedDraft);
      if (await page.locator('[data-review-upload]').count()) {
        assert.equal(await page.locator('#review-bulk-actions').isVisible(), false);
        await page.locator('[data-review-upload]').click();
        await assertStep('source-upload-slot');
        await assertScope('AUTHORITATIVE', expectedDraft);
        await chooseStep('step-review');
      }
      await metrics(name + '-review');
      await shot(name + '-03-review');
      await page.locator('[data-panel="explore"]').first().click();
      await page.locator('[data-panel="build"]').click();
      await assertStep('step-review');
      await assertScope('AUTHORITATIVE', expectedDraft);
      await chooseStep('step-publication');
      await assertScope('AUTHORITATIVE', expectedDraft);
      if (await page.locator('[data-publication-review]').count()) {
        assert.equal(await page.locator('#publication-submit-actions').isVisible(), false);
        assert.equal(await page.locator('#publication-output').isVisible(), false);
        await page.locator('[data-publication-review]').click();
        await assertStep('step-review');
        await assertScope('AUTHORITATIVE', expectedDraft);
        await chooseStep('step-publication');
      }
      await metrics(name + '-publication');
      await shot(name + '-04-publication');
      await page.locator('[data-panel="explore"]').first().click();
      await page.locator('[data-panel="build"]').click();
      await assertStep('step-publication');
      await assertScope('AUTHORITATIVE', expectedDraft);

      await chooseStep('source-upload-slot');
      await page.locator('#document-knowledge-scope').selectOption('BUSINESS');
      await assertScope('BUSINESS', expectedDraft);
      await chooseStep('step-review');
      await assertScope('BUSINESS', expectedDraft);
      await chooseStep('step-publication');
      await assertScope('BUSINESS', expectedDraft);
      await chooseStep('source-upload-slot');
      await page.locator('#inspect-build-ontology').click();
      assert.equal(await page.locator('#expert-foundation').isVisible(), true);
      assert.equal(await page.locator('#instance-workspace').isVisible(), false);
      assert.equal(await page.locator('#ontology-editor').isVisible(), true);
      const ontologyDraft = await page.locator('#ontology-editor').inputValue();
      await page.locator('#ontology-editor').fill(ontologyDraft + '\n');
      await toTop();
      await metrics(name + '-ontology');
      await shot(name + '-05-ontology');
      await page.locator('[data-panel="explore"]').first().click();
      await page.locator('[data-panel="build"]').click();
      assert.equal(await page.locator('#expert-foundation').isVisible(), true, 'sidebar return preserves ontology tab');
      await page.locator('#build-tab-ontology').focus();
      await page.keyboard.press('ArrowRight');
      assert.equal(await page.locator('#build-tab-instances').getAttribute('aria-selected'), 'true');
      assert.equal(await page.locator('#build-tab-instances').evaluate(node => node === document.activeElement), true);
      await assertScope('BUSINESS', expectedDraft);
      await chooseStep('step-review');
      await page.locator('#build-tab-ontology').click();
      assert.equal(await page.locator('#ontology-editor').inputValue(), ontologyDraft + '\n', 'ontology draft survives tab switches');
      await page.locator('#build-tab-instances').click();
      await assertStep('step-review');
      await chooseStep('source-upload-slot');
      await page.locator('#ontology-selection > summary').click();
      const ontologyKey = await page.locator('#document-tbox').inputValue();
      await page.locator('#document-tbox').fill(ontologyKey + '-draft');
      assert.match(await page.locator('#foundation-status').textContent(), /未启用/);
      await page.locator('#document-tbox').fill(ontologyKey);
      assert.match(await page.locator('#foundation-status').textContent(), / · v/);
      await page.locator('#ontology-selection > summary').click();
      await assertScope('BUSINESS', expectedDraft);
      if (name === 'wide-desktop') {
        // Exercise fullscreen CSS layout without adding a product control or submitting data.
        await page.evaluate(() => document.documentElement.requestFullscreen());
        await page.waitForFunction(() => !!document.fullscreenElement);
        await toTop();
        await metrics(name + '-fullscreen');
        await shot(name + '-06-fullscreen');
        await page.evaluate(() => document.exitFullscreen());
        await page.waitForFunction(() => !document.fullscreenElement);
      }
    }
    assert.deepEqual(results.blockedRequests, [], 'unexpected write, corpus or external requests were blocked');
    assert.deepEqual(results.pageErrors, [], 'no browser JavaScript exceptions');
    assert.deepEqual(results.consoleErrors, [], 'no browser console errors');
    assert.deepEqual(results.failedResponses, [], 'all application requests must succeed');
    results.status = 'passed';
    await fs.rm(path.join(output, 'failure.png'), { force: true });
    console.log(JSON.stringify({ status: results.status, output, screenshots: results.screenshots.length, observations: results.observations.length }, null, 2));
  } catch (error) {
    results.status = 'failed';
    results.failure = { message: error.message, stack: error.stack };
    await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {});
    throw error;
  } finally {
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify(results, null, 2) + '\n');
    await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
