fnnas-sdk.js —— fnOS 前端 SDK（penpal / app-auth 桥）的构建产物。

用途：让应用页面能调用 fnOS 桌面的原生目录选择器
      （SDK 实例方法 pickFile / pickUserFile / pickSharedFile / authorizeUserFile）。

来源：本机已安装应用 file.dedupe 2.x 的 www/assets/fnnas-*.js（fnOS 官方 SDK 构建产物，
      被该应用打了自己的 appName 与回调路径；iframe + penpal 通道与 appName 无关，
      本应用只使用 pickSharedFile）。

注意：本文件仅供随应用分发使用。若要上架应用中心，建议换成官方渠道分发的 SDK 包，
      并按其版本更新；替换时只需保持导出名 s（SDK 类）、o（pickSharedFile 封装）、
      r（错误类）不变，页面引用无需改动。
