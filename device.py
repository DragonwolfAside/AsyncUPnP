import asyncio
import os
import re
from typing import Union

import aiohttp
import netifaces
import urllib.parse
from ssdpy import SSDPClient
from xml.dom import minidom
from xml.etree import ElementTree
from lxml import etree
from ssdpy.protocol import create_msearch_payload
from ssdpy.http_helper import parse_headers


async def find_gateways():
    default = netifaces.gateways()['default']
    return [default[i][0] for i in default]


def get_host(url):
    return '/'.join(url.split('/')[:-1])


def _substitute_newlines_with_space(match):
    s, e = match.span()
    return match.string[s:e].replace('\n', ' ')


class DIRECTION:
    In = 0
    Out = 1


class ISSDPClient(SSDPClient):
    async def m_search(self, st="ssdp:all", mx=1):
        host = "{}:{}".format(self.broadcast_ip, self.port)
        data = create_msearch_payload(host, st, mx)
        self.send(data)
        for response in self.recv():
            try:
                headers = parse_headers(response)
                yield headers
            except ValueError:
                pass
        print(" * Finished SSDP Discovery")
        return


class SOAP:
    def __init__(self, target, action, **act_args):
        self.loc = target
        self.action = action
        self.args = act_args

    def construct(self):
        root = ElementTree.Element('s:Envelope')
        root.set('xmlns:s', 'http://schemas.xmlsoap.org/soap/envelope/')
        root.set('s:encodingStyle', 'http://schemas.xmlsoap.org/soap/encoding/')

        xml_body = ElementTree.SubElement(root, 's:Body')

        action_name = ElementTree.SubElement(xml_body, f'u:{self.action.name}')
        action_name.set('xmlns:u', self.action.parent.parent.type)

        for arg in self.action.in_args:
            try:
                val = self.args[arg]
            except KeyError:
                continue

            action_argument = ElementTree.SubElement(action_name, arg.name)
            action_argument.text = str(val)

        body = ElementTree.tostring(root, short_empty_elements=False)

        header = {
            'Host': f'{urllib.parse.urlparse(self.action.parent.parent.loc).netloc}',
            'Content-Length': str(len(body)),
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPAction': f'"{self.action.parent.parent.type}#{self.action.name}"'
        }

        return body, header

    async def send(self):
        body, header = self.construct()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.action.parent.parent.control_url,
                data=body, headers=header
            ) as result:
                resp = await result.text()

        resp = re.sub('(?<=>)[\\s\\t\\n]+(?=<)', '', resp)
        resp = re.sub('<[^<>]+>', _substitute_newlines_with_space, resp)
        root = minidom.parseString(resp)
        args = root.getElementsByTagNameNS('*', 'Body')[0]  \
            .getElementsByTagNameNS('*', self.action.name + 'Response')[0]

        ret_args = {}

        for i in args.childNodes:
            if i.firstChild is None:
                ret_args[i.tagName] = ''
            else:
                ret_args[i.tagName] = i.firstChild.nodeValue

        return ret_args


class SSDPStateVariable:
    def __init__(self, name: str, send: bool, type_: str):
        self.name = name
        self.send = send
        self.type = type_


class SSDPArgument:
    def __init__(self, name: str, direction: int, state: SSDPStateVariable):
        self.name = name
        self.direction = direction
        self.state = state


class SSDPAction:
    def __init__(self, parent, xml):
        self.parent = parent
        self.name = xml.getElementsByTagName('name')[0].firstChild.nodeValue
        self.arguments = {}

        for arg in xml.getElementsByTagName('argument'):
            name = arg.getElementsByTagName('name')[0].firstChild.nodeValue
            direction = arg.getElementsByTagName('direction')[0].firstChild.nodeValue.lower()
            direction = DIRECTION.In if direction == 'in' else DIRECTION.Out
            state_name = arg.getElementsByTagName('relatedStateVariable')[0].firstChild.nodeValue
            state = self.parent.state_variable[state_name]
            self.arguments[name] = SSDPArgument(name, direction, state)

    def __repr__(self):
        return (f"<SSDPAction name={self.name} in={tuple([i for i in self.in_args])}"
                f" out={tuple([i for i in self.out_args])}>")

    @property
    def in_args(self):
        return [i for i in self.arguments if self.arguments[i].direction == DIRECTION.In]

    @property
    def out_args(self):
        return [i for i in self.arguments if self.arguments[i].direction == DIRECTION.Out]

    async def do_action(self, **args):
        if not all(arg.name in args.keys() for arg in self.in_args):
            print(f"Insufficient arguments: {[arg for arg in self.in_args if arg not in args.keys()]}")

        return await SOAP(self.parent.loc, self, **args).send()


class SSDPActions:
    def __init__(self, parent,
                 location: Union[str, os.PathLike, None] = None,
                 xml: Union[str, bytes, bytearray, memoryview] = None):
        self.parent = parent
        self.loc = location
        self.root = minidom.parseString(xml)
        self._xml = etree.fromstring(xml)
        self._ns = '{' + self._xml.xpath('namespace-uri(.)') + '}' if self._xml.xpath('namespace-uri(.)') is not None \
            else ''

        self._actions = {}
        self.state_variable = {}

    def __repr__(self):
        return f"<SSDPActions actions={[i for i in self._actions.keys()]}>"

    def __iter__(self):
        return [self._actions[i] for i in self._actions].__iter__()

    def __getitem__(self, item):
        return self._actions[item]

    @classmethod
    async def from_url(cls, parent, loc):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(loc) as response:
                    if response.status != 200:
                        print(f"Error fetching {loc}: {response.status}")
                        return
                    else:
                        obj = cls(parent, loc, await response.read())
        except Exception as error:
            print(f"Failed fetching {loc} : {error}")
            return

        for var in obj.root.getElementsByTagName('stateVariable'):
            name = var.getElementsByTagName('name')[0].firstChild.nodeValue
            send_event = var.getAttributeNode('sendEvents').nodeValue
            data_type = var.getElementsByTagName('dataType')[0].firstChild.nodeValue
            obj.state_variable[name] = SSDPStateVariable(name, send_event, data_type)

        for act in obj.root.getElementsByTagName('action'):
            name = act.getElementsByTagName('name')[0].firstChild.nodeValue
            obj._actions[name] = SSDPAction(obj, act)

        return obj


class SSDPService:
    actions: any

    def __init__(self, location: Union[str, os.PathLike], xml: Union[str, bytes, bytearray, memoryview]):
        self.loc = location
        self.root = minidom.parseString(xml)
        self._xml = etree.fromstring(xml)
        self._ns = '{' + self._xml.xpath('namespace-uri(.)') + '}' if self._xml.xpath('namespace-uri(.)') is not None \
            else ''

        self.type = self.root.getElementsByTagName('serviceType')[0].firstChild.nodeValue
        self.id = self.root.getElementsByTagName('serviceId')[0].firstChild.nodeValue

        self.SCPD_url = get_host(self.loc) + self.root.getElementsByTagName('SCPDURL')[0].firstChild.nodeValue
        self.control_url = get_host(self.loc) + self.root.getElementsByTagName('controlURL')[0].firstChild.nodeValue
        self.event_url = get_host(self.loc) + self.root.getElementsByTagName('eventSubURL')[0].firstChild.nodeValue

        self.actions = None

    @classmethod
    async def from_url(cls, location: Union[str, os.PathLike], xml: Union[str, bytes, bytearray, memoryview]):
        obj = cls(location, xml)
        obj.actions = await SSDPActions.from_url(obj, obj.SCPD_url)
        return obj

    def __repr__(self):
        return f'<SSDPService loc={self.loc} type={self.type}>'


class SSDPDevice:
    services: list[SSDPService]

    def __init__(self, location: Union[str, os.PathLike], xml: Union[str, bytes, bytearray, memoryview]):
        self.loc = location

        self.root = minidom.parseString(xml)
        self._xml = etree.fromstring(xml)
        self._ns = '{' + self._xml.xpath('namespace-uri(.)') + '}' if self._xml.xpath('namespace-uri(.)') is not None \
            else ''

        self._device = self._xml.find(f'{self._ns}device')
        self.type = self.root.getElementsByTagName('deviceType')[0].firstChild.nodeValue
        self.name = self.root.getElementsByTagName('friendlyName')[0].firstChild.nodeValue
        self.services = []

    def __repr__(self):
        return f'<SSDPDevice loc={self.loc} type={self.type}>'

    @classmethod
    async def from_url(cls, loc):
        async with aiohttp.ClientSession() as session:
            async with session.get(loc) as response:
                if response.status != 200:
                    print(f"Error fetching {loc}: {response.status}")
                    return
                obj = cls(loc, await response.read())

        for svr in obj.root.getElementsByTagName('service'):
            obj.services.append(await SSDPService.from_url(obj.loc, svr.toxml()))
        return obj

    def find_service(self, service_type):
        for service in self.services:
            if service_type == service.type:
                return service

    def trace(self):
        print(f"Device: {self.name}")
        for service in self.services:
            print(f" + Service: {service.type}")
            for action in service.actions:
                print(f" | + Action: {action.name} in:{tuple([i for i in action.in_args])}"
                      f" out:{tuple([i for i in action.out_args])}")


async def search_all():
    cli = ISSDPClient()
    remotes = []
    async for i in cli.m_search():
        if not i['location'] in remotes:
            remotes.append(i['location'])
            yield SSDPDevice.from_url(i['location'])
        else:
            continue


async def main():
    async for i in search_all():
        device = await i
        print(device)


if __name__ == '__main__':
    asyncio.run(main())
    input("Program Exited...")
