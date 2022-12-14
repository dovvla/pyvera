import os
import warnings
import json
from datetime import datetime
from collections import defaultdict
from jinja2 import Environment, FileSystemLoader

from silvera.generator.registration import GeneratorDesc
from silvera.const import HOST_CONTAINER, HTTP_POST, HTTP_PUT
from silvera.core import (
    CustomType,
    ConfigServerDecl,
    ServiceRegistryDecl,
    ServiceDecl,
    APIGateway,
    TypeDef,
    ConsumerAnnotation,
)
from silvera.core import TypedList, TypeDef, TypedSet, TypedDict, Function


def get_root_path():
    """Returns project's root path."""
    path = os.path.split(os.path.abspath(os.path.dirname(__file__)))[0]
    return path


def get_templates_path():
    """Returns the path to the templates folder."""
    return os.path.join(get_root_path(), "python_gen", "templates")


def timestamp():
    return "{:%Y-%m-%d %H:%M:%S}".format(datetime.now())


def upper_case(string):
    return string[0].upper() + string[1:]


def lower_case(string):
    return string[0].lower() + string[1:]


def all_lower(string):
    return string.lower()


def get_params(func: Function):
    params = [f"{p.name}: {convert_type(p.type)}" for p in func.params]
    if func.http_verb in {HTTP_POST, HTTP_PUT}:
        params.append(f"dto: dict")
    return ", ".join(params)


def get_params_wo_types(func: Function):
    params = [f"{p.name}" for p in func.params]
    return ", ".join(params)


def convert_type(_type):
    def _convert_type(_type):
        if isinstance(_type, TypeDef):
            return upper_case(_type.name)
        if isinstance(_type, TypedList):
            return f"List[{_convert_type(_type.type)}]"
        if isinstance(_type, TypedSet):
            return f"Set[{_convert_type(_type.type)}]"
        if isinstance(_type, TypedDict):
            return f"Dict[{_convert_type(_type.key_type)}, {_convert_type(_type.value_type)}]"
        return _type

    return _convert_type(_type)


def silvera_type_to_pydantic(field_type):
    primitives_map = {
        "date": "date",
        "int": "int",
        "float": "float",
        "double": "float",
        "str": "str",
        "list": "list",
        "set": "set",
        "dict": "dict",
        "bool": "bool",
    }
    return (
        primitives_map[field_type]
        if field_type in primitives_map
        else convert_type(field_type)
    )


def silvera_type_to_pydantic_default(field_type):
    primitives_map = {
        "date": "datetime.now()",
        "int": "0",
        "float": "0.0",
        "double": "0.0",
        "str": "",
        "list": "[]",
        "set": "\{\}",
        "dict": "\{\}",
        "void": "",
        "bool": "False",
    }
    if field_type in primitives_map:
        return primitives_map[field_type]
    _type = convert_type(field_type)
    if _type.startswith("List["):
        return "[]"
    if _type.startswith("Dict[") or _type.startswith("Set["):
        return "\{\}"
    return _type + "()"


class ServiceGenerator:
    def __init__(self, service: ServiceDecl, output_dir):
        self.service = service
        self.env = self._init_env()

    def _init_env(self):
        env = Environment(loader=FileSystemLoader(get_templates_path()))

        env.filters["upper_case"] = upper_case
        env.filters["lower_case"] = lower_case
        env.filters["all_lower"] = all_lower
        env.filters["get_params"] = lambda f: get_params(f)
        env.filters["get_params_wo_types"] = lambda f: get_params_wo_types(f)
        env.filters["silvera_type_to_pydantic"] = lambda t: silvera_type_to_pydantic(t)
        env.filters[
            "silvera_type_to_pydantic_default"
        ] = lambda t: silvera_type_to_pydantic_default(t)
        env.globals["service_name"] = self.service.name
        env.filters["is_primitive_type"] = lambda t: is_primitive_type(t)
        env.globals[
            "header"
        ] = lambda: f"\n\tGenerated by: silvera\n\tDatetime: {datetime.now()}\n"

        return env

    def generate_model(self):
        if not os.path.exists(f"output/{upper_case(self.service.name)}"):
            os.makedirs(f"output/{upper_case(self.service.name)}")
        open(f"output/{upper_case(self.service.name)}/openapi.json", "a").close()
        for typedef in self.service.api.typedefs + self.service.dep_typedefs:
            id_field = (
                "id"
                if [f for f in typedef.fields if f.isid] == []
                else [f for f in typedef.fields if f.isid][0].name
            )
            class_template = self.env.get_template("model.j2")
            if not os.path.exists(f"{self.service.name}/models"):
                os.makedirs(f"{self.service.name}/models")
            class_template.stream(
                {"typedef": typedef, "id_field": id_field, "api": self.service.api}
            ).dump(
                os.path.join(
                    f"{self.service.name}/models", lower_case(typedef.name) + ".py"
                )
            )

    def generate_api(self):
        for typedef in self.service.api.typedefs:
            id_field = (
                "id"
                if [f for f in typedef.fields if f.isid] == []
                else [f for f in typedef.fields if f.isid][0].name
            )
            class_template = self.env.get_template("api.j2")
            if not os.path.exists(f"{self.service.name}/api"):
                os.makedirs(f"{self.service.name}/api")
            class_template.stream(
                {"typedef": typedef, "id_field": id_field, "api": self.service.api}
            ).dump(
                os.path.join(
                    f"{self.service.name}/api", lower_case(typedef.name) + ".py"
                )
            )

    def generate_messaging(self):
        if not os.path.exists(f"{self.service.name}/messaging"):
            os.makedirs(f"{self.service.name}/messaging")
        class_template = self.env.get_template("messaging/messaging.j2")
        try:
            functions = [func for func in self.service.api.internal.functions]
        except:
            functions = []
        topics = set()
        message_per_function = set()
        function_per_channel = defaultdict(set)
        for function in functions:
            for annotation in function.msg_annotations:
                if isinstance(annotation, ConsumerAnnotation):
                    for sub in annotation.subscriptions:
                        topics.add(sub.channel.name)
                        message_per_function.add(function.name)
                        function_per_channel[sub.channel].add(function.name)
        self.topics = '","'.join(topics)
        class_template.stream({"messaging": '","'.join(topics)}).dump(
            os.path.join(f"{self.service.name}/messaging", "messaging" + ".py")
        )
        messages = []
        for group in self.service.parent.model.msg_pool.groups:
            messages += [message for message in group.messages]
        class_template = self.env.get_template("messaging/message_models.j2")
        class_template.stream({"messages": messages}).dump(
            os.path.join(f"{self.service.name}/messaging", "message_models" + ".py")
        )

        class_template = self.env.get_template("messaging/consumer_methods.j2")
        class_template.stream(
            {
                "message_per_function": message_per_function,
                "function_per_channel": function_per_channel,
                "topics": topics,
            }
        ).dump(
            os.path.join(f"{self.service.name}/messaging", "consumer_methods" + ".py")
        )

    def generate_functions(self):
        class_template = self.env.get_template("functions.j2")
        if not os.path.exists(f"{self.service.name}/api"):
            os.makedirs(f"{self.service.name}/api")
        class_template.stream({"api": self.service.api}).dump(
            os.path.join(f"{self.service.name}/api", "functions" + ".py")
        )

    def generate_dep_service(self):
        class_template = self.env.get_template("dep_service.j2")
        if not os.path.exists(f"{self.service.name}/external"):
            os.makedirs(f"{self.service.name}/external")
        class_template.stream(
            {
                "api": self.service.api,
                "functions": self.service.dep_functions,
                "typedefs": self.service.api.typedefs + self.service.dep_typedefs,
            }
        ).dump(os.path.join(f"{self.service.name}/external", "dep_service" + ".py"))

    def generate_consul(self):
        class_template = self.env.get_template("consul.j2")
        if not os.path.exists(f"{self.service.name}/external"):
            os.makedirs(f"{self.service.name}/external")
        class_template.stream({}).dump(
            os.path.join(f"{self.service.name}/external", "consul" + ".py")
        )

    def generate_main(self):
        class_template = self.env.get_template("main.j2")
        class_template.stream(
            {
                "typedefs": self.service.api.typedefs,
                "service": self.service,
                "messaging": self.topics,
            }
        ).dump(os.path.join(f"{self.service.name}", "main" + ".py"))

    def generate_run_script(self):
        class_template = self.env.get_template("run.j2")
        # print("called")
        class_template.stream({"service": self.service, "sh": "#!/bin/sh"}).dump(
            os.path.join(f"{self.service.name}", "run" + ".sh")
        )

    def format_everything(self):
        import subprocess

        subprocess.run(["black", "."])

    def generate_requirements(self):
        class_template = self.env.get_template("requirements.j2")
        class_template.stream({}).dump(
            os.path.join(f"{self.service.name}", "requirements" + ".txt")
        )

    def generate_run_script(self):
        class_template = self.env.get_template("dockerfile.j2")
        # print("called")
        class_template.stream({"service": self.service, "sh": "#!/bin/sh"}).dump(
            os.path.join(f"{self.service.name}", "Dockerfile" + "")
        )

    def generate(self):
        self.generate_model()
        self.generate_api()
        self.generate_messaging()
        self.generate_functions()
        self.generate_dep_service()
        self.generate_consul()
        self.generate_main()
        self.generate_requirements()
        self.generate_run_script()
        self.generate_dockerfile()
        # rest of gens
        self.format_everything()


def generate_service(service, output_dir):
    generator = ServiceGenerator(service, output_dir)
    generator.generate()


def generate_api_gateway(api_gateway, output_dir):
    env = Environment(loader=FileSystemLoader(get_templates_path()))
    class_template = self.env.get_template("nginx.j2")
    import random

    print("GATEWAY")
    class_template.stream(
        {
            "port": api_gateway.port or random.randint(50000, 60000),
            "gateway_for": [
                (
                    g.service.name,
                    g.service.port,
                    g.path,
                )
                for g in api_gateway.gateway_for
            ],
        }
    ).dump(os.path.join(f"/", "gateway" + ".conf"))
    pass


def generate_service_registry():
    pass


def generate_config_server():
    pass


_obj_to_fnc = {
    # ConfigServerDecl: generate_config_server,
    # ServiceRegistryDecl: generate_service_registry,
    ServiceDecl: generate_service,
    # APIGateway: generate_api_gateway,
}


def generate(decl, output_dir, debug):
    """Entry point function for code generator.

    Args:
        decl(Decl): can be declaration of service registry or config
                    server.
        output_dir(str): output directory.
        debug(bool): True if debug mode activated. False otherwise.
    """

    # print("Called!")
    print(decl)
    try:
        fnc = _obj_to_fnc[decl.__class__]
        fnc(decl, output_dir)
    except:
        pass


python = GeneratorDesc(
    language_name="python",
    language_ver="3.10",
    description="Python 3.10 code generator",
    gen_func=generate,
)
