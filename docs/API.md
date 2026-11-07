# HTTP API（首版）

根路径 `/api`，JSON请求/响应。先GET `/bootstrap`获得当前登录状态与CSRF；所有POST包含 `X-CSRF-Token`。会话采用HttpOnly、SameSite=Lax cookie；HTTPS模式额外Secure。无登录返回401，无权限403，业务状态冲突409，输入错误400。上传照片时用multipart，其余一般为JSON。SSE使用同源cookie。

|接口|方法|用途|
|---|---|---|
|/health|GET|服务时间与SQLite版本|
|/bootstrap|GET|角色、权限内员工、区域版本、配置、本人设备与会话|
|/login、/logout|POST|登录/退出；登录也需先取CSRF|
|/password、/profile|POST|本人密码、本人姓名|
|/monitor|GET|当前人员状态；负责人过滤旧班组位置|
|/stream|GET|text/event-stream，refresh事件；每连接25秒后重连全量复查|
|/sessions/start|POST|area_id,purpose,device_id,client_key,tab_id，可选collector_token恢复本端|
|/sessions/{id}/end|POST|reason，可选client_requested_at；本人或管理员|
|/positions|POST|collector_token及1—500个points，逐条accepted/duplicate/rejected|
|/heartbeat|POST|当前账号页面联系时间，不能当作有效位置|
|/tracks|GET|user_id,start,end,limit及after_time/after_id游标，左闭右开时段|
|/allocations|POST|user_id,area_id，负责人或管理员分配|
|/transfers|GET/POST|任务列表/直接下达；POST含user_id,from_area,to_area,allowed_minutes|
|/transfers/{id}/{action}|POST|start、arrive、extend、confirm、reject、cancel|
|/events|GET|权限内事件及证据/处理时间线|
|/events/{id}/action|POST|action=explain/investigate/close，text必填|
|/help|GET/POST|权限内求助/员工提交；client_id幂等，category,description，可选photo|
|/help/{id}/photo|GET|受案例权限约束的照片|
|/help/{id}/action|POST|accept/progress/resolve/confirm/reopen/assist/assign及text|
|/notifications|GET|本人消息|
|/notifications/read|POST|ids数组，只能标记本人消息|
|/reports|GET/POST|快照列表/生成；start,end|
|/reports/{id}、/{id}.csv|GET|本人生成快照或管理员访问；旧快照不修改|
|/evaluations|GET/POST|管理员重复性统计；user_id,start,end,source,conditions,test_type，可选planned_samples；route必填reference_start/reference_end|
|/admin/state|GET|管理基础资料（不含密码哈希）|
|/admin/action|POST|管理命令，见下|
|/admin/import-employees|POST|multipart CSV，逐行反馈|
|/audit|GET|管理员最近500条审计|
|/admin/backup|POST|创建备份并独立恢复校验|
|/admin/backups/{id}|GET|管理员下载对应ZIP|
|/admin/import-simulation|POST|count=10000/100000，固定同族输入前缀去重|
|/admin/simulation|POST|running=true/false，开启/停止进程内模拟流|
|/admin/maintenance|POST|手动执行一次中断/超时/督办检查|

管理命令 `action`：team_change、team_create、leader_assign、user_enable、reset_password、user_create、device_bind、area_version、settings、calibration、shift。字段以 `src/minepath/management.py` 的服务端校验为准，审计排除password字段。

浏览器点：point_id、observed_at（epoch秒或带时区时间）、lat、lng、accuracy。服务端决定source与设备/会话归属，忽略客户端自填平面坐标或来源。模拟点仅由内部签发simulation租约提供x、y。观测区间必须在会话/采集租约有效期内；截止时刻本身不接收，旧有效期补传不恢复已终止会话。

系统不会把迟到点按接收时间写成新位置；较旧记录标记late_review并保留历史版本，供人工核查。SSE只是“刷新提示”，重连后客户端重新GET数据；它不声称逐条事件流已被客户端完整消费。

历史轨迹接口v1.1保持JSON字段不变；响应头`X-Track-Cache: hit/miss`用于检查内部短时缓存。身份与权限每次重新校验，数据库提交使旧缓存失效；浏览器侧仍为Cache-Control: no-store。

## v1.2 底图接口

- GET /api/site-maps：登录后读取current和versions元数据，包括图片尺寸、origin_x/y、meters_per_pixel、bounds、生效区间、reason及image_url。
- POST /api/admin/site-map：管理员及CSRF校验。multipart上传file（PNG/JPG≤5MB）；name、origin_x、origin_y、meters_per_pixel、reason、base_id。初次base_id为空；修改必须提交正在编辑版本的id。不换图时也可用JSON更新比例/位置。并发版本冲突返回409，非法内容返回400/413。
- GET /api/site-maps/<asset_id>/image：登录后读取规范化PNG。
- bootstrap新增site_maps；monitor新增site_maps和area_history。既有字段及tracks响应保持不变。区域绘制仍调用POST /api/admin/action，action=area_version。

## 巡检与求助代处理

- GET /api/inspections，读取当前角色权限内的巡检列表。
- POST /api/inspections，负责人/管理员提交 user_id、area_id、note 指派任务。
- GET /api/inspections/<id>，读取任务、检查记录及当前作业区相关信息。
- POST /api/inspections/<id>/start，受派员工开始巡检。
- POST /api/inspections/<id>/finding，提交 target（area/worker）、problem（布尔值）、description、category，可按人员检查指定 subject_user_id。
- POST /api/inspections/<id>/complete，至少一条检查记录后完成。
- POST /api/inspections/<id>/cancel，管理人员填写 reason 撤回。
- 求助动作新增 proxy_accept、proxy_progress、proxy_resolve，由符合当前巡检/主责校验的员工执行；管理员重新交接主责时清除代处理关系。

写接口均需登录和 CSRF。详细字段与错误状态以 web.py 的当前实现为准。
