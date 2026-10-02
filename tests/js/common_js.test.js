/*
 * 共享前端脚本的单元测试（node tests/js/common_js.test.js）。
 *
 * 直接用 vm 加载 app/templates/partials/common_js.html 里的真实代码，只补齐
 * 浏览器环境里被用到的几个接口：document.createElement（用于 escapeHtml）、
 * getElementById、localStorage、fetch。断言重点是 L5 的转义行为——尤其是
 * escapeAttr 必须比 escapeHtml 多转义引号，否则属性值能被闭合出来。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TEMPLATE = path.join(__dirname, '..', '..', 'app', 'templates', 'partials', 'common_js.html');
const source = fs.readFileSync(TEMPLATE, 'utf8');
const js = source.replace(/^\s*<script>\s*/, '').replace(/<\/script>\s*$/, '');

// 浏览器里 div.textContent = X; div.innerHTML 的转义规则：转义 & < >，不转义引号
// （这正是 escapeAttr 存在的理由）。
function browserEscape(text) {
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const boxes = {};
const sandbox = {
    console,
    localStorage: {
        _data: {},
        getItem(key) { return Object.prototype.hasOwnProperty.call(this._data, key) ? this._data[key] : null; },
        setItem(key, value) { this._data[key] = String(value); },
    },
    fetch: async () => { throw new Error('模拟网络失败'); },
    document: {
        createElement() {
            let text = '';
            return {
                set textContent(value) { text = value == null ? '' : String(value); },
                get textContent() { return text; },
                get innerHTML() { return browserEscape(text); },
            };
        },
        getElementById(id) { return boxes[id] || null; },
    },
};
vm.createContext(sandbox);
vm.runInContext(js, sandbox);

let failed = 0;
function check(name, ok, detail) {
    if (ok) {
        console.log('[OK] ' + name);
    } else {
        failed += 1;
        console.log('[FAIL] ' + name + (detail ? '   ' + detail : ''));
    }
}
function equal(name, got, want) {
    check(name, got === want, 'got=' + JSON.stringify(got) + ' want=' + JSON.stringify(want));
}

// ---- 代码真的被加载了 ----
for (const fn of ['escapeHtml', 'escapeAttr', 'r18Param', 'showLoadError', 'fetchJson']) {
    equal('加载到共享函数 ' + fn, typeof sandbox[fn], 'function');
}

// ---- L5：escapeHtml ----
equal('转义尖括号', sandbox.escapeHtml('<script>alert(1)</script>'), '&lt;script&gt;alert(1)&lt;/script&gt;');
equal('转义 & 号', sandbox.escapeHtml('a & b'), 'a &amp; b');
equal('先转义 & 再转义尖括号（不产生二次转义）', sandbox.escapeHtml('&lt;'), '&amp;lt;');
equal('空文本返回空串', sandbox.escapeHtml(''), '');
equal('null / undefined 安全', sandbox.escapeHtml(null) + sandbox.escapeHtml(undefined), '');
equal('普通文本原样', sandbox.escapeHtml('标题 A'), '标题 A');

// ---- L5 的核心：属性值必须比文本多转义引号 ----
equal('escapeHtml 不转义引号（浏览器行为）', sandbox.escapeHtml('"x"'), '"x"');
equal('escapeAttr 转义双引号（防属性闭合）',
    sandbox.escapeAttr('" onmouseover="alert(1)'),
    '&quot; onmouseover=&quot;alert(1)');
equal('escapeAttr 转义单引号', sandbox.escapeAttr("it's"), 'it&#39;s');
equal('escapeAttr 同时转义尖括号与 &', sandbox.escapeAttr('<a & b>'), '&lt;a &amp; b&gt;');
equal('escapeAttr 空值安全', sandbox.escapeAttr(null), '');

// ---- r18Param ----
equal('r18 未设置时为空串', sandbox.r18Param(), '');
sandbox.localStorage.setItem('pa-r18', 'hide');
equal('r18=hide 透传', sandbox.r18Param(), 'hide');
sandbox.localStorage.setItem('pa-r18', 'only');
equal('r18=only 透传', sandbox.r18Param(), 'only');
sandbox.localStorage.setItem('pa-r18', '其他值');
equal('r18 非法值被忽略', sandbox.r18Param(), '');

// ---- showLoadError：收起「加载中」并把原因写进目标容器 ----
boxes.loading = { style: {} };
boxes.detail = { style: {}, textContent: '' };
sandbox.showLoadError(new Error('网络断了'));
equal('showLoadError 收起了加载中', boxes.loading.style.display, 'none');
equal('showLoadError 显示出失败原因', boxes.detail.textContent, '加载失败：网络断了');
equal('showLoadError 让容器可见', boxes.detail.style.display, 'block');

(async () => {
    // ---- fetchJson：失败时返回 null 而不是抛出 ----
    const result = await sandbox.fetchJson('/api/whatever');
    equal('fetchJson 失败时返回 null', result, null);
    equal('fetchJson 失败时也提示了原因', boxes.detail.textContent, '加载失败：模拟网络失败');
    console.log(failed ? 'FAILED=' + failed : 'ALL_PASSED');
    process.exit(failed ? 1 : 0);
})();