/* 局域网访问令牌助手
 * - 仅从当前 URL ?token= 读取令牌
 * - 全局 fetch 自动附加 X-Access-Token 请求头
 * - paImg(url) 为原图/需鉴权的 img 标签地址附加 ?token=
 */
(function () {
    var urlToken = '';
    try {
        urlToken = new URLSearchParams(window.location.search).get('token') || '';
    } catch (e) {
        urlToken = '';
    }
    var t = urlToken;
    try {
        localStorage.removeItem('pa-lan-token');
        document.cookie = 'pa_lan_token=; path=/; max-age=0; SameSite=Lax';
    } catch (e) {}
    window.paToken = t;

    var orig = window.fetch ? window.fetch.bind(window) : null;
    if (orig) {
        window.fetch = function (url, opts) {
            opts = opts || {};
            if (t) {
                var headers = new Headers(opts.headers || {});
                headers.set('X-Access-Token', t);
                opts.headers = headers;
            }
            return orig(url, opts);
        };
    }

    window.paImg = function (url) {
        if (t) {
            return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(t);
        }
        return url;
    };
})();
