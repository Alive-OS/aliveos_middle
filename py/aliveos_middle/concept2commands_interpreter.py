# *************************************************************************
#
# Copyright (c) 2021 Andrei Gramakov. All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# site:    https://agramakov.me
# e-mail:  mail@agramakov.me
#
# *************************************************************************

from threading import Lock
from time import sleep
from typing import Union

from rospy import logdebug, logerr, loginfo, logwarn, init_node, Service, spin

from aliveos_app import node_types
from aliveos_app.ego_node import EGO_COMMANDS
from aliveos_msgs import msg, srv
from aliveos_py import ConstantNamespace
from aliveos_py.helpers.json_tools import json_to_dict, string_to_obj
from aliveos_py.ros import get_publisher, get_server, get_subscriber, get


class MultiLock:
    def __init__(self):
        self.acc_count = 0

    def acquire(self):
        self.acc_count += 1
        return True

    def release(self):
        if self.acc_count:
            self.acc_count -= 1
            return True
        return False

    def locked(self):
        return bool(self.acc_count)


class C2C_SPECIFIC_CONCEPTS(ConstantNamespace):
    WAIT = "wait"


class C2C_RESPONSE(ConstantNamespace):
    ERROR = "error"
    OK = "ok"
    BUSY = "busy"
    ABORT = "abort"


class Concept2CommandsInterpreter:
    def __init__(self):
        self.lock_exec_ego = Lock()
        self.lock_exec_instinct = Lock()
        self.lockmult_exec_reflex = MultiLock()
        self.executing_reflex_concepts = []
        self.data = {}
        self.concepts = {}

        # Servers
        self.srv_command_concepts = None  # type: Union[Service, None]
        self.srv_command_concept_descriptors = None  # type: Union[Service, None]
        # Publishers
        self.pub_device_cmd = None
        self.pub_ego_commands = None
        # Subscribers
        self.sub_perception_concepts = None

    def publish_device_cmd(self, device: str, cmd: str, arg: str):
        to_send = msg.DeviceCmd()
        to_send.device = device
        to_send.cmd = cmd
        to_send.arg = arg
        self.pub_device_cmd.publish(to_send)
        logdebug(f"c2c -> dev: {device} - {cmd}({arg})")

    def publish_ego_cmd(self, command):
        if not EGO_COMMANDS.contains(command):
            logerr(f"Wrong Ego Command: {command}")
            return
        to_send = msg.EgoCommands()
        to_send.cmd = command
        self.pub_ego_commands.publish(to_send)
        logdebug(f"-> Ego command: {command}")

    def get_concept_dsc(self, concept: str) -> Union[list, None]:
        c = self.concepts.get(concept)
        if c:
            return c
        logerr(f"Unknown concept: {concept}")
        return None

    def exec_concept(self, symbol: str, modifier: str) -> Union[str, None]:
        """
        Parameters
        ----------
        symbol : str
            [description]
        modifier : str
            [description]

        Returns
        -------
        Union[str, None]
            [description]
        """
        commands = None
        if modifier == "()":
            modifier = ""
        concept_dsc = self.get_concept_dsc(symbol)
        if not concept_dsc:
            return None
        for c in concept_dsc:
            if c["modifier"] is modifier:
                commands = c["commands"]
                break
        if not commands:
            msg = f"There is no such modifier: {modifier}"
            logerr(msg)
            return f"Error: {msg}"

        for cmd in commands:
            logdebug(f"c2c -> dev : {cmd}")
            cmd_name = cmd["command"]
            cmd_dev = cmd["device_name"]
            cmd_args = cmd.get("arguments", "")

            if C2C_SPECIFIC_CONCEPTS.contains(cmd_name):
                cmd_method = getattr(self, f"command_{cmd_name}", None)
                cmd_method(cmd_args)
            else:
                self.publish_device_cmd(device=cmd_dev, cmd=cmd_name, arg=str(cmd_args))

        return C2C_RESPONSE.OK

    @staticmethod
    def command_wait(mods):
        for m in mods:
            try:
                value = float(m)
                sleep(value)
                return C2C_RESPONSE.OK
            except ValueError:
                pass
        logerr("Wait concept has now duration argument!")
        return C2C_RESPONSE.ERROR

    def exec_concept_from_ego(self, symbol, mods):
        if self.lock_exec_ego.acquire():
            res = self.exec_concept(symbol, mods)
            if self.lock_exec_ego.locked():
                self.lock_exec_ego.release()
        else:
            res = C2C_RESPONSE.BUSY
        return res

    def pause_ego(self):
        logdebug("pause_ego")
        self.publish_ego_cmd(EGO_COMMANDS.PAUSE)

    def reset_ego(self):
        logdebug("reset_ego")
        if self.lock_exec_ego.locked():
            self.lock_exec_ego.release()
        self.publish_ego_cmd(EGO_COMMANDS.RESET)

    def unpause_ego(self):
        logdebug("unpause_ego")
        self.publish_ego_cmd(EGO_COMMANDS.CONTINUE)

    def exec_concept_from_instinct(self, symbol, mods):
        if self.lock_exec_instinct.acquire():
            self.pause_ego()
            self.reset_ego()
            res = self.exec_concept(symbol, mods)
            self.unpause_ego()
            self.lock_exec_instinct.release()
        else:
            res = C2C_RESPONSE.BUSY
        return res

    def exec_concept_from_reflex(self, symbol, mods):
        self.lockmult_exec_reflex.acquire()
        res = self.exec_concept(symbol, mods)
        self.lockmult_exec_reflex.release()
        return res

    def handler_command_concept(self, req: srv.CommandConceptRequest) -> srv.CommandConceptResponse:
        res = ""
        concept = req.symbol
        mods = req.modifier
        node_type = req.ego_type
        logdebug("mind -> c2c: %s, mods: %s, type: %d" % (concept, mods, node_type))

        if node_type == node_types.EGO_NODE:
            res = self.exec_concept_from_ego(concept, mods)
        elif node_type == node_types.INSTINCT_NODE:
            res = self.exec_concept_from_instinct(concept, mods)
        elif node_type == node_types.REFLEX_NODE:
            res = self.exec_concept_from_reflex(concept, mods)

        res = C2C_RESPONSE.OK
        return srv.CommandConceptResponse(result=res)

    def handler_command_concept_descriptor(
            self, req: srv.CommandConceptDescriptorRequest) -> srv.CommandConceptDescriptorResponse:
        logdebug("handler_command_concept_descriptor: %s" % req.descriptor_json)
        json_dict = json_to_dict(req.descriptor_json)

        name = json_dict["name"]
        dsc = json_dict["descriptor"]
        if self.concepts.get(name):
            logerr(f"Command concept {name} already exists!")
            return srv.CommandConceptDescriptorResponse(result=C2C_RESPONSE.ERROR)
        self.concepts[name] = dsc
        return srv.CommandConceptDescriptorResponse(result=C2C_RESPONSE.OK)

    def handler_perception_concept(self, data: msg.PerceptionConcept):
        logdebug("d2c -> c2c: %s:%s" % (data.symbol, data.modifier))
        self.data[data.symbol] = data.modifier
        logdebug(self.data)

    def init_communications(self):
        self.pub_device_cmd = get_publisher.device_cmd()
        self.pub_ego_commands = get_publisher.ego_commands()
        self.srv_command_concepts = get_server.command_concept(self.handler_command_concept)
        self.srv_command_concept_descriptors = get_server.command_concept_descriptor(
            self.handler_command_concept_descriptor)
        self.sub_perception_concepts = get_subscriber.perception_concept(self.handler_perception_concept)

    def start(self):
        init_node(name=self.__class__.__name__, anonymous=False)
        self.init_communications()
        while (not get.param("FLAG_EGO_READY")):
            sleep(.1)
            logwarn("Ego is not ready yet!")
        self.unpause_ego()


def start():
    obj = Concept2CommandsInterpreter()
    obj.start()
    spin()
