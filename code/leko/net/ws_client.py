"""net：WebSocket 客户端 ↔ ECS Robot Gateway —— 未实装（Phase 1 步 3）。

部署目标（已定）：ECS root@8.161.228.205（免密已配，ssh config 里的 Host）。
到货步骤：ECS 上 dsh --profile headless + robot-gateway 插件（WSS 服务端），
本模块实现 WSS 长连接 + 断线指数退避重连 + 离线降级（本地技能照常）。
协议：RobotCommand{id, type: move|photo|speak|stop, payload, timeout}
      RobotCommandResult{id, success, result?, error?}   # 指令-回执关联
      RobotEvent{type: wake|obstacle|task_done|..., payload, timestamp}
"""
