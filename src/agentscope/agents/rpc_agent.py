# -*- coding: utf-8 -*-
""" Base class for Rpc Agent """

from multiprocessing import Process, Event, Pipe
from multiprocessing.synchronize import Event as EventClass
import socket
import threading
import json
import base64
import traceback
import asyncio
from typing import Type, Optional, Union, Sequence
from concurrent import futures
from loguru import logger

try:
    import dill
    import grpc
    from grpc import ServicerContext
    from expiringdict import ExpiringDict
except ImportError as import_error:
    from agentscope.utils.tools import ImportErrorReporter

    dill = ImportErrorReporter(import_error, "distribute")
    grpc = ImportErrorReporter(import_error, "distribute")
    ServicerContext = ImportErrorReporter(import_error, "distribute")
    ExpiringDict = ImportErrorReporter(import_error, "distribute")

from agentscope._init import init_process, _INIT_SETTINGS
from agentscope.agents.agent import AgentBase
from agentscope.message import (
    Msg,
    PlaceholderMessage,
    deserialize,
    serialize,
)
from agentscope.rpc import (
    RpcAgentClient,
    RpcMsg,
    RpcAgentServicer,
    add_RpcAgentServicer_to_server,
)


def rpc_servicer_method(  # type: ignore[no-untyped-def]
    func,
):
    """A decorator used to identify that the specific method is an rpc agent
    servicer method, which can only be run in the rpc server process.
    """

    def inner(rpc_agent, msg):  # type: ignore[no-untyped-def]
        if not rpc_agent.is_servicer:
            error_msg = f"Detect main process try to use rpc servicer method \
                 [{func.__name__}]"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        return func(rpc_agent, msg)

    return inner


class RpcAgent(AgentBase):
    """A wrapper to extend an AgentBase into a gRPC Client."""

    def __init__(
        self,
        name: str,
        host: str = "localhost",
        port: int = None,
        agent_class: Type[AgentBase] = None,
        agent_configs: Optional[dict] = None,
        max_pool_size: int = 8192,
        max_timeout_seconds: int = 1800,
        local_mode: bool = True,
        lazy_launch: bool = True,
        agent_id: str = None,
        connect_existing: bool = False,
    ) -> None:
        """Initialize a RpcAgent instance.

        Args:
            name (`str`): the name of the agent.
            host (`str`, defaults to `localhost`):
                Hostname of the rpc agent server.
            port (`int`, defaults to `None`):
                Port of the rpc agent server.
            agent_class (`Type[AgentBase]`):
                the AgentBase subclass of the source agent.
            agent_configs (`dict`): The args used to
                initialize the agent, generated by `_AgentMeta`.
            max_pool_size (`int`, defaults to `8192`):
                Max number of task results that the server can accommodate.
            max_timeout_seconds (`int`, defaults to `1800`):
                Timeout for task results.
            local_mode (`bool`, defaults to `True`):
                Whether the started gRPC server only listens to local
                requests.
            lazy_launch (`bool`, defaults to `True`):
                Only launch the server when the agent is called.
            agent_id (`str`, defaults to `None`):
                The agent id of this instance. If `None`, it will
                be generated randomly.
            connect_existing (`bool`, defaults to `False`):
                Set to `True`, if the agent is already running on the agent
                server.
        """
        super().__init__(name=name)
        self.agent_class = agent_class
        self.agent_configs = agent_configs
        self.host = host
        self.port = port
        self.server_launcher = None
        self.client = None
        self.connect_existing = connect_existing
        if agent_id is not None:
            self._agent_id = agent_id
        # if host and port are not provided, launch server locally
        launch_server = port is None
        if launch_server:
            self.host = "localhost"
            self.server_launcher = RpcAgentServerLauncher(
                host=self.host,
                port=port,
                max_pool_size=max_pool_size,
                max_timeout_seconds=max_timeout_seconds,
                local_mode=local_mode,
                custom_agents=[agent_class],
            )
            if not lazy_launch:
                self._launch_server()
        else:
            self.client = RpcAgentClient(
                host=self.host,
                port=self.port,
                agent_id=self.agent_id,
            )
            if not self.connect_existing:
                self.client.create_agent(agent_configs)

    def _launch_server(self) -> None:
        """Launch a rpc server and update the port and the client"""
        self.server_launcher.launch()
        self.port = self.server_launcher.port
        self.client = RpcAgentClient(
            host=self.host,
            port=self.port,
            agent_id=self.agent_id,
        )
        self.client.create_agent(self.agent_configs)

    def reply(self, x: dict = None) -> dict:
        if self.client is None:
            self._launch_server()
        return PlaceholderMessage(
            name=self.name,
            content=None,
            client=self.client,
            x=x,
        )

    def observe(self, x: Union[dict, Sequence[dict]]) -> None:
        if self.client is None:
            self._launch_server()
        self.client.call_func(
            func_name="_observe",
            value=serialize(x),  # type: ignore[arg-type]
        )

    def clone_instances(
        self,
        num_instances: int,
        including_self: bool = True,
    ) -> Sequence[AgentBase]:
        """
        Clone a series of this instance with different agent_id and
        return them as a list.

        Args:
            num_instances (`int`): The number of instances in the returned
            list.
            including_self (`bool`): Whether to include the instance calling
            this method in the returned list.

        Returns:
            `Sequence[AgentBase]`: A list of agent instances.
        """
        generated_instance_number = (
            num_instances - 1 if including_self else num_instances
        )
        generated_instances = []

        # launch the server before clone instances
        if self.client is None:
            self._launch_server()

        # put itself as the first element of the returned list
        if including_self:
            generated_instances.append(self)

        # clone instances without agent server
        for _ in range(generated_instance_number):
            new_agent_id = self.client.call_func("_clone_agent")
            generated_instances.append(
                RpcAgent(
                    name=self.name,
                    host=self.host,
                    port=self.port,
                    agent_id=new_agent_id,
                    connect_existing=True,
                ),
            )
        return generated_instances

    def stop(self) -> None:
        """Stop the RpcAgent and the rpc server."""
        if self.server_launcher is not None:
            self.server_launcher.shutdown()

    def __del__(self) -> None:
        self.stop()


def setup_rpc_agent_server(
    host: str,
    port: int,
    init_settings: dict = None,
    start_event: EventClass = None,
    stop_event: EventClass = None,
    pipe: int = None,
    local_mode: bool = True,
    max_pool_size: int = 8192,
    max_timeout_seconds: int = 1800,
    custom_agents: list = None,
) -> None:
    """Setup gRPC server rpc agent.

    Args:
        host (`str`, defaults to `"localhost"`):
            Hostname of the rpc agent server.
        port (`int`):
            The socket port monitored by grpc server.
        init_settings (`dict`, defaults to `None`):
            Init settings for agentscope.init.
        start_event (`EventClass`, defaults to `None`):
            An Event instance used to determine whether the child process
            has been started.
        stop_event (`EventClass`, defaults to `None`):
            The stop Event instance used to determine whether the child
            process has been stopped.
        pipe (`int`, defaults to `None`):
            A pipe instance used to pass the actual port of the server.
        local_mode (`bool`, defaults to `None`):
            Only listen to local requests.
        max_pool_size (`int`, defaults to `8192`):
            Max number of task results that the server can accommodate.
        max_timeout_seconds (`int`, defaults to `1800`):
            Timeout for task results.
        custom_agents (`list`, defaults to `None`):
            A list of custom agent classes that are not in `agentscope.agents`.
    """
    asyncio.run(
        setup_rpc_agent_server_async(
            host=host,
            port=port,
            init_settings=init_settings,
            start_event=start_event,
            stop_event=stop_event,
            pipe=pipe,
            local_mode=local_mode,
            max_pool_size=max_pool_size,
            max_timeout_seconds=max_timeout_seconds,
            custom_agents=custom_agents,
        ),
    )


async def setup_rpc_agent_server_async(
    host: str,
    port: int,
    init_settings: dict = None,
    start_event: EventClass = None,
    stop_event: EventClass = None,
    pipe: int = None,
    local_mode: bool = True,
    max_pool_size: int = 8192,
    max_timeout_seconds: int = 1800,
    custom_agents: list = None,
) -> None:
    """Setup gRPC server rpc agent in an async way.

    Args:
        host (`str`, defaults to `"localhost"`):
            Hostname of the rpc agent server.
        port (`int`):
            The socket port monitored by grpc server.
        init_settings (`dict`, defaults to `None`):
            Init settings for agentscope.init.
        start_event (`EventClass`, defaults to `None`):
            An Event instance used to determine whether the child process
            has been started.
        stop_event (`EventClass`, defaults to `None`):
            The stop Event instance used to determine whether the child
            process has been stopped.
        pipe (`int`, defaults to `None`):
            A pipe instance used to pass the actual port of the server.
        local_mode (`bool`, defaults to `None`):
            Only listen to local requests.
        max_pool_size (`int`, defaults to `8192`):
            Max number of task results that the server can accommodate.
        max_timeout_seconds (`int`, defaults to `1800`):
            Timeout for task results.
        custom_agents (`list`, defaults to `None`):
            A list of custom agent classes that are not in `agentscope.agents`.
    """

    if init_settings is not None:
        init_process(**init_settings)
    servicer = AgentPlatform(
        host=host,
        port=port,
        max_pool_size=max_pool_size,
        max_timeout_seconds=max_timeout_seconds,
    )
    # update agent registry
    if custom_agents is not None:
        for agent_class in custom_agents:
            AgentBase.register_agent_class(agent_class=agent_class)
    while True:
        try:
            port = check_port(port)
            servicer.port = port
            logger.info(
                f"Starting rpc server at port [{port}]...",
            )
            server = grpc.aio.server(
                futures.ThreadPoolExecutor(max_workers=None),
            )
            add_RpcAgentServicer_to_server(servicer, server)
            if local_mode:
                server.add_insecure_port(f"localhost:{port}")
            else:
                server.add_insecure_port(f"0.0.0.0:{port}")
            await server.start()
            break
        except OSError:
            logger.warning(
                f"Failed to start rpc server at port [{port}]"
                f"try another port",
            )
    logger.info(
        f"rpc server at port [{port}] started successfully",
    )
    if start_event is not None:
        pipe.send(port)
        start_event.set()
        while not stop_event.is_set():
            await asyncio.sleep(1)
        logger.info(
            f"Stopping rpc server at port [{port}]",
        )
        await server.stop(10.0)
    else:
        await server.wait_for_termination()
    logger.info(
        f"rpc server at port [{port}] stopped successfully",
    )


def find_available_port() -> int:
    """Get an unoccupied socket port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def check_port(port: Optional[int] = None) -> int:
    """Check if the port is available.

    Args:
        port (`int`):
            the port number being checked.

    Returns:
        `int`: the port number that passed the check. If the port is found
        to be occupied, an available port number will be automatically
        returned.
    """
    if port is None:
        new_port = find_available_port()
        logger.warning(
            "gRpc server port is not provided, automatically select "
            f"[{new_port}] as the port number.",
        )
        return new_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", port)) == 0:
            new_port = find_available_port()
            logger.warning(
                f"Port [{port}] is occupied, use [{new_port}] instead",
            )
            return new_port
    return port


class RpcAgentServerLauncher:
    """The launcher of AgentPlatform (formerly RpcAgentServer)."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = None,
        max_pool_size: int = 8192,
        max_timeout_seconds: int = 1800,
        local_mode: bool = False,
        custom_agents: list = None,
        agent_class: Type[AgentBase] = None,
        agent_args: tuple = (),
        agent_kwargs: dict = None,
    ) -> None:
        """Init a rpc agent server launcher.

        Args:
            host (`str`, defaults to `"localhost"`):
                Hostname of the rpc agent server.
            port (`int`, defaults to `None`):
                Port of the rpc agent server.
            max_pool_size (`int`, defaults to `8192`):
                Max number of task results that the server can accommodate.
            max_timeout_seconds (`int`, defaults to `1800`):
                Timeout for task results.
            local_mode (`bool`, defaults to `False`):
                Whether the started rpc server only listens to local
                requests.
            custom_agents (`list`, defaults to `None`):
                A list of custom agent classes that are not in
                `agentscope.agents`.
            agent_class (`Type[AgentBase]`, deprecated):
                The AgentBase subclass encapsulated by this wrapper.
            agent_args (`tuple`, deprecated): The args tuple used to
                initialize the agent_class.
            agent_kwargs (`dict`, deprecated): The args dict used to
                initialize the agent_class.
        """
        self.host = host
        self.port = check_port(port)
        self.max_pool_size = max_pool_size
        self.max_timeout_seconds = max_timeout_seconds
        self.local_mode = local_mode
        self.server = None
        self.stop_event = None
        self.parent_con = None
        self.custom_agents = custom_agents
        if (
            agent_class is not None
            or len(agent_args) > 0
            or agent_kwargs is not None
        ):
            logger.warning(
                "`agent_class`, `agent_args` and `agent_kwargs` is deprecated"
                " in `RpcAgentServerLauncher`",
            )

    def _launch_in_main(self) -> None:
        """Launch gRPC server in main-process"""
        logger.info(
            f"Launching agent server at [{self.host}:{self.port}]...",
        )
        asyncio.run(
            setup_rpc_agent_server_async(
                host=self.host,
                port=self.port,
                max_pool_size=self.max_pool_size,
                max_timeout_seconds=self.max_timeout_seconds,
                local_mode=self.local_mode,
                custom_agents=self.custom_agents,
            ),
        )

    def _launch_in_sub(self) -> None:
        """Launch gRPC server in sub-process."""
        self.stop_event = Event()
        self.parent_con, child_con = Pipe()
        start_event = Event()
        server_process = Process(
            target=setup_rpc_agent_server,
            kwargs={
                "host": self.host,
                "port": self.port,
                "init_settings": _INIT_SETTINGS,
                "start_event": start_event,
                "stop_event": self.stop_event,
                "pipe": child_con,
                "max_pool_size": self.max_pool_size,
                "max_timeout_seconds": self.max_timeout_seconds,
                "local_mode": self.local_mode,
                "custom_agents": self.custom_agents,
            },
        )
        server_process.start()
        self.port = self.parent_con.recv()
        start_event.wait()
        self.server = server_process
        logger.info(
            f"Launch agent server at [{self.host}:{self.port}] success",
        )

    def launch(self, in_subprocess: bool = True) -> None:
        """launch a rpc agent server.

        Args:
            in_subprocess (bool, optional): launch the server in subprocess.
                Defaults to True. For agents that need to obtain command line
                input, such as UserAgent, please set this value to False.
        """
        if in_subprocess:
            self._launch_in_sub()
        else:
            self._launch_in_main()

    def wait_until_terminate(self) -> None:
        """Wait for server process"""
        if self.server is not None:
            self.server.join()

    def shutdown(self) -> None:
        """Shutdown the rpc agent server."""
        if self.server is not None:
            if self.stop_event is not None:
                self.stop_event.set()
                self.stop_event = None
            self.server.join()
            if self.server.is_alive():
                self.server.kill()
                logger.info(
                    f"Agent server at port [{self.port}] is killed.",
                )
            self.server = None


class AgentPlatform(RpcAgentServicer):
    """A platform for agent to run on (formerly RpcServerSideWrapper)"""

    def __init__(
        self,
        host: str = "localhost",
        port: int = None,
        max_pool_size: int = 8192,
        max_timeout_seconds: int = 1800,
    ):
        """Init the AgentPlatform.

        Args:
            host (`str`, defaults to "localhost"):
                Hostname of the rpc agent server.
            port (`int`, defaults to `None`):
                Port of the rpc agent server.
            max_pool_size (`int`, defaults to `8192`):
                The max number of task results that the server can
                accommodate. Note that the oldest result will be deleted
                after exceeding the pool size.
            max_timeout_seconds (`int`, defaults to `1800`):
                Timeout for task results. Note that expired results will be
                deleted.
        """
        self.host = host
        self.port = port
        self.result_pool = ExpiringDict(
            max_len=max_pool_size,
            max_age_seconds=max_timeout_seconds,
        )
        self.executor = futures.ThreadPoolExecutor(max_workers=None)
        self.task_id_lock = threading.Lock()
        self.agent_id_lock = threading.Lock()
        self.task_id_counter = 0
        self.agent_pool: dict[str, AgentBase] = {}

    def get_task_id(self) -> int:
        """Get the auto-increment task id."""
        with self.task_id_lock:
            self.task_id_counter += 1
            return self.task_id_counter

    def agent_exists(self, agent_id: str) -> bool:
        """Check whether the agent exists.

        Args:
            agent_id (`str`): the agent id.

        Returns:
            bool: whether the agent exists.
        """
        return agent_id in self.agent_pool

    def check_and_generate_agent(
        self,
        agent_id: str,
        agent_configs: dict,
    ) -> None:
        """
        Check whether the agent exists, and create new agent instance
        for new agent.

        Args:
            agent_id (`str`): the agent id.
            agent_configs (`dict`): configuration used to initialize the agent,
                with three fields (generated in `_AgentMeta`):

                .. code-block:: python

                    {
                        "class_name": {name of the agent}
                        "args": {args in tuple type to init the agent}
                        "kwargs": {args in dict type to init the agent}
                    }

        """
        with self.agent_id_lock:
            if agent_id not in self.agent_pool:
                agent_class_name = agent_configs["class_name"]
                agent_instance = AgentBase.get_agent_class(agent_class_name)(
                    *agent_configs["args"],
                    **agent_configs["kwargs"],
                )
                agent_instance._agent_id = agent_id  # pylint: disable=W0212
                self.agent_pool[agent_id] = agent_instance
                logger.info(f"create agent instance [{agent_id}]")

    def check_and_delete_agent(self, agent_id: str) -> None:
        """
        Check whether the agent exists, and delete the agent instance
        for the agent_id.

        Args:
            agent_id (`str`): the agent id.
        """
        with self.agent_id_lock:
            if agent_id in self.agent_pool:
                self.agent_pool.pop(agent_id)
                logger.info(f"delete agent instance [{agent_id}]")

    def call_func(  # pylint: disable=W0236
        self,
        request: RpcMsg,
        context: ServicerContext,
    ) -> RpcMsg:
        """Call the specific servicer function."""
        if hasattr(self, request.target_func):
            if request.target_func not in ["_create_agent", "_get"]:
                if not self.agent_exists(request.agent_id):
                    return context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT,
                        f"Agent [{request.agent_id}] not exists.",
                    )
            return getattr(self, request.target_func)(request)
        else:
            # TODO: support other user defined method
            logger.error(f"Unsupported method {request.target_func}")
            return context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"Unsupported method {request.target_func}",
            )

    def _reply(self, request: RpcMsg) -> RpcMsg:
        """Call function of RpcAgentService

        Args:
            request (`RpcMsg`):
                Message containing input parameters or input parameter
                placeholders.

        Returns:
            `RpcMsg`: A serialized Msg instance with attributes name, host,
            port and task_id
        """
        if request.value:
            msg = deserialize(request.value)
        else:
            msg = None
        task_id = self.get_task_id()
        self.result_pool[task_id] = threading.Condition()
        self.executor.submit(
            self.process_messages,
            task_id,
            request.agent_id,
            msg,  # type: ignore[arg-type]
        )
        return RpcMsg(
            value=Msg(
                name=self.agent_pool[request.agent_id].name,
                content=None,
                task_id=task_id,
            ).serialize(),
        )

    def _get(self, request: RpcMsg) -> RpcMsg:
        """Get function of RpcAgentService

        Args:
            request (`RpcMsg`):
                Identifier of message, with json format::

                {
                    'task_id': int
                }

        Returns:
            `RpcMsg`: Concrete values of the specific message (or part of it).
        """
        msg = json.loads(request.value)
        while True:
            result = self.result_pool.get(msg["task_id"])
            if isinstance(result, threading.Condition):
                with result:
                    result.wait(timeout=1)
            else:
                break
        return RpcMsg(value=result.serialize())

    def _observe(self, request: RpcMsg) -> RpcMsg:
        """Observe function of RpcAgentService

        Args:
            request (`RpcMsg`):
                The serialized input to be observed.

        Returns:
            `RpcMsg`: Empty RpcMsg.
        """
        msgs = deserialize(request.value)
        for msg in msgs:
            if isinstance(msg, PlaceholderMessage):
                msg.update_value()
        self.agent_pool[request.agent_id].observe(msgs)
        return RpcMsg()

    def _create_agent(self, request: RpcMsg) -> RpcMsg:
        """Create a new agent instance for the agent_id.

        Args:
            request (RpcMsg): request message with a `agent_id` field.
        """
        self.check_and_generate_agent(
            request.agent_id,
            agent_configs=(
                dill.loads(base64.b64decode(request.value))
                if request.value
                else None
            ),
        )
        return RpcMsg()

    def _clone_agent(self, request: RpcMsg) -> RpcMsg:
        """Clone a new agent instance from the origin instance.

        Args:
            request (RpcMsg): The `agent_id` field is the agent_id of the
            agent to be cloned.

        Returns:
            `RpcMsg`: The `value` field contains the agent_id of generated
            agent.
        """
        agent_id = request.agent_id
        with self.agent_id_lock:
            if agent_id not in self.agent_pool:
                raise ValueError(f"Agent [{agent_id}] not exists")
            ori_agent = self.agent_pool[agent_id]
        new_agent = ori_agent.__class__(
            *ori_agent._init_settings["args"],  # pylint: disable=W0212
            **ori_agent._init_settings["kwargs"],  # pylint: disable=W0212
        )
        with self.agent_id_lock:
            self.agent_pool[new_agent.agent_id] = new_agent
        return RpcMsg(value=new_agent.agent_id)

    def _delete_agent(self, request: RpcMsg) -> RpcMsg:
        """Delete the agent instance of the specific sesssion_id.

        Args:
            request (RpcMsg): request message with a `agent_id` field.
        """
        self.check_and_delete_agent(request.agent_id)
        return RpcMsg()

    def process_messages(
        self,
        task_id: int,
        agent_id: str,
        task_msg: dict = None,
    ) -> None:
        """Task processing."""
        if isinstance(task_msg, PlaceholderMessage):
            task_msg.update_value()
        cond = self.result_pool[task_id]
        try:
            result = self.agent_pool[agent_id].reply(task_msg)
            self.result_pool[task_id] = result
        except Exception:
            error_msg = traceback.format_exc()
            logger.error(f"Error in agent [{agent_id}]:\n{error_msg}")
            self.result_pool[task_id] = Msg(
                name="ERROR",
                role="assistant",
                __status="ERROR",
                content=f"Error in agent [{agent_id}]:\n{error_msg}",
            )
        with cond:
            cond.notify_all()
