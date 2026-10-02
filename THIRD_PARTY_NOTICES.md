# Third-Party Notices

本项目使用了以下第三方开源组件。本仓库以 GPL-3.0 发布；各依赖库按各自许可证授权，均为与 GPL-3.0 兼容的许可证。

## Referenced/derived design

| 项目 | 许可证 | 说明 |
|------|--------|------|
| PixEz / pixez-flutter (Notsfsssf) | MIT（老版）/ GPL-3.0（Flutter 版） | 直连模块（免 SNI、域名→IP 覆盖表、DoH 解析）的实现思路源自该项目；已获作者许可性声明，在此致谢：Notsfsssf |
| pixivpy / pixivpy3 | Unlicense | Pixiv AppAPI 客户端库 |

## Runtime dependencies (requirements.txt)

| 包 | 许可证 |
|----|--------|
| fastapi | MIT |
| hypercorn | MIT |
| pixivpy3 | Unlicense |
| Pillow | MIT-CMU (HPND) |
| Jinja2 | BSD-3-Clause |
| python-dotenv | BSD-3-Clause |
| aiofiles | Apache-2.0 |
| pywebview | BSD-3-Clause |
| pystray | LGPL-3.0（仅作未修改依赖使用） |

(上述包各自的传递依赖如 starlette、pydantic、requests、urllib3、pystray 等亦为 MIT / BSD / Apache / PSF / MPL 等宽松许可以及 LGPL-3.0，详见各包随附的许可证文本。)

## Build time (requirements-build.txt)

| 包 | 许可证 |
|----|--------|
| PyInstaller | GPL-2.0-or-later，**含特殊例外**，允许使用其构建的产品按任何许可证分发 |

## 再分发注意

- MIT / BSD 系依赖要求再分发时保留其版权声明，请在随发布产物一起分发时保留相应包的 LICENSE/NOTICE（或在本仓库的依赖清单中注明）。
- pystray（LGPL-3.0）仅以库形式动态引用、未做修改，允许在 GPL-3.0 项目中使用；若你修改了 pystray 本身，修改部分需按 LGPL 发布。
- 图片内容版权归各 Pixiv 作者所有。