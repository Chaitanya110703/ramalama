from pathlib import Path
import argparse
import glob
import json
import os
import subprocess
import sys
import time
import atexit

from ramalama.huggingface import Huggingface
from ramalama.common import (
    container_manager,
    default_image,
    find_working_directory,
    genname,
    in_container,
    perror,
    run_cmd,
)
from ramalama.oci import OCI
from ramalama.ollama import Ollama
from ramalama.shortnames import Shortnames
from ramalama.version import version, print_version

shortnames = Shortnames()


class HelpException(Exception):
    pass


def use_container():
    transport = os.getenv("RAMALAMA_IN_CONTAINER")
    if transport:
        return transport.lower() == "true"

    if in_container():
        return False

    return container_manager()


class ArgumentParserWithDefaults(argparse.ArgumentParser):
    def add_argument(self, *args, help=None, default=None, **kwargs):
        if help is not None:
            kwargs['help'] = help
        if default is not None and args[0] != '-h':
            kwargs['default'] = default
            if help is not None and help != "==SUPPRESS==":
                kwargs['help'] += ' (default: {})'.format(default)
        super().add_argument(*args, **kwargs)


def init_cli():
    description = """\
RamaLama tool facilitates local management and serving of AI Models.

On first run RamaLama inspects your system for GPU support, falling back to CPU support if no GPUs are present.

RamaLama uses container engines like Podman or Docker to pull the appropriate OCI image with all of the software \
necessary to run an AI Model for your systems setup.

Running in containers eliminates the need for users to configure the host system for AI. After the initialization, \
RamaLama runs the AI Models within a container based on the OCI image.

RamaLama then pulls AI Models from model registires. Starting a chatbot or a rest API service from a simple single \
command. Models are treated similarly to how Podman and Docker treat container images.

When both Podman and Docker are installed, RamaLama defaults to Podman, The `RAMALAMA_CONTAINER_ENGINE=docker` \
environment variable can override this behaviour. When neather are installed RamaLama will attempt to run the model \
with software on the local system.
"""
    parser = ArgumentParserWithDefaults(
        prog="ramalama",
        description=description,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--container",
        dest="container",
        default=use_container(),
        action="store_true",
        help="""run RamaLama in the default container.
The RAMALAMA_IN_CONTAINER environment variable modifies default behaviour.""",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="display debug messages",
    )
    parser.add_argument(
        "--dryrun", dest="dryrun", action="store_true", help="show container runtime command without executing it"
    )
    parser.add_argument("--dry-run", dest="dryrun", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--engine",
        dest="engine",
        default=container_manager(),
        help="""run RamaLama using the specified container engine.
The RAMALAMA_CONTAINER_ENGINE environment variable modifies default behaviour.""",
    )
    parser.add_argument(
        "--image",
        default=default_image(),
        help="OCI container image to run with specified AI model",
    )
    parser.add_argument(
        "--nocontainer",
        dest="container",
        default=False,
        action="store_false",
        help="""do not run RamaLama in the default container.
The RAMALAMA_IN_CONTAINER environment variable modifies default behaviour.""",
    )
    parser.add_argument(
        "--runtime",
        default="llama.cpp",
        choices=["llama.cpp", "vllm"],
        help="specify the runtime to use, valid options are 'llama.cpp' and 'vllm'",
    )
    parser.add_argument("--store", default=get_store(), help="store AI Models in the specified directory")
    parser.add_argument("-v", dest="version", action="store_true", help="show RamaLama version")

    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.required = False

    help_parser(subparsers)
    containers_parser(subparsers)
    info_parser(subparsers)
    list_parser(subparsers)
    login_parser(subparsers)
    logout_parser(subparsers)
    pull_parser(subparsers)
    push_parser(subparsers)
    rm_parser(subparsers)
    run_parser(subparsers)
    serve_parser(subparsers)
    stop_parser(subparsers)
    version_parser(subparsers)
    # Parse CLI
    args = parser.parse_args()

    # create stores directories
    mkdirs(args.store)
    if hasattr(args, "MODEL"):
        resolved_model = shortnames.resolve(args.MODEL)
        if resolved_model:
            args.UNRESOLVED_MODEL = args.MODEL
            args.MODEL = resolved_model

    return parser, args


def login_parser(subparsers):
    parser = subparsers.add_parser("login", help="login to remote registry")
    # Do not run in a container
    parser.add_argument("--authfile", help="path of the authentication file")
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.add_argument("-p", "--password", dest="password", help="password for registry")
    parser.add_argument(
        "--password-stdin", dest="passwordstdin", action="store_true", help="take the password for registry from stdin"
    )
    parser.add_argument(
        "--tls-verify",
        dest="tlsverify",
        default=True,
        help="require HTTPS and verify certificates when contacting registries",
    )
    parser.add_argument("--token", dest="token", help="token for registry")
    parser.add_argument("-u", "--username", dest="username", help="username for registry")
    parser.add_argument(
        "REGISTRY", nargs="?", type=str, default="OCI Registry where AI models are stored"
    )  # positional argument
    parser.set_defaults(func=login_cli)


def login_cli(args):
    registry = args.REGISTRY
    if registry != "":
        registry = "oci://" + registry

    model = New(str(registry), args)
    return model.login(args)


def logout_parser(subparsers):
    parser = subparsers.add_parser("logout", help="logout from remote registry")
    # Do not run in a container
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.add_argument("--token", help="token for registry")
    parser.add_argument("TRANSPORT", nargs="?", type=str, default="")  # positional argument
    parser.add_argument("TRANSPORT", nargs="?", type=str, default="")  # positional argument
    parser.set_defaults(func=logout_cli)


def logout_cli(args):
    transport = args.TRANSPORT
    if transport != "":
        transport = os.getenv("RAMALAMA_TRANSPORT") + "://"
    model = New(str(transport), args)
    return model.logout(args)


def mkdirs(store):
    # List of directories to create
    directories = [
        "models/huggingface",
        "repos/huggingface",
        "models/oci",
        "repos/oci",
        "models/ollama",
        "repos/ollama",
    ]

    # Create each directory
    for directory in directories:
        full_path = os.path.join(store, directory)
        os.makedirs(full_path, exist_ok=True)


def human_duration(d):
    if d < 1:
        return "Less than a second"
    elif d == 1:
        return "1 second"
    elif d < 60:
        return f"{d} seconds"
    elif d < 120:
        return "1 minute"
    elif d < 3600:
        return f"{d // 60} minutes"
    elif d < 7200:
        return "1 hour"
    elif d < 86400:
        return f"{d // 3600} hours"
    elif d < 172800:
        return "1 day"
    elif d < 604800:
        return f"{d // 86400} days"
    elif d < 1209600:
        return "1 week"
    elif d < 2419200:
        return f"{d // 604800} weeks"
    elif d < 4838400:
        return "1 month"
    elif d < 31536000:
        return f"{d // 2419200} months"
    elif d < 63072000:
        return "1 year"
    else:
        return f"{d // 31536000} years"


def list_files_by_modification():
    return sorted(Path().rglob("*"), key=lambda p: os.path.getmtime(p), reverse=True)


def containers_parser(subparsers):
    parser = subparsers.add_parser("containers", aliases=["ps"], help="list all RamaLama containers")
    parser.add_argument("--format", help="pretty-print containers to JSON or using a Go template")
    parser.add_argument("-n", "--noheading", dest="noheading", action="store_true", help="do not display heading")
    parser.add_argument("--no-trunc", dest="notrunc", action="store_true", help="display the extended information")
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.set_defaults(func=list_containers)


def _list_containers(args):
    conman = args.engine
    if conman == "":
        raise IndexError("no container manager (Podman, Docker) found")

    conman_args = [conman, "ps", "-a", "--filter", "label=RAMALAMA"]
    if args.noheading:
        conman_args += ["--noheading"]
    if hasattr(args, "notrunc") and args.notrunc:
        conman_args += ["--no-trunc"]

    if args.format:
        conman_args += [f"--format={args.format}"]

    try:
        output = run_cmd(conman_args).stdout.decode("utf-8").strip()
        if output == "":
            return []
        return output.split("\n")
    except subprocess.CalledProcessError as e:
        perror("ramalama list command requires a running container engine")
        raise (e)


def list_containers(args):
    if len(_list_containers(args)) == 0:
        return
    print("\n".join(_list_containers(args)))


def info_parser(subparsers):
    parser = subparsers.add_parser("info", help="Display information pertaining to setup of RamaLama.")
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.set_defaults(func=info_cli)


def list_parser(subparsers):
    parser = subparsers.add_parser("list", aliases=["ls"], help="list all downloaded AI Models")
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.add_argument("-n", "--noheading", dest="noheading", action="store_true", help="do not display heading")
    parser.add_argument("--json", dest="json", action="store_true", help="print using json")
    parser.add_argument("-q", "--quiet", dest="quiet", action="store_true", help="print only Model names")
    parser.set_defaults(func=list_cli)


def human_readable_size(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            size = round(size, 2)
            return f"{size} {unit}"

        size /= 1024

    return f"{size} PB"


def get_size(file):
    return human_readable_size(os.path.getsize(file))


def _list_models(args):
    mycwd = os.getcwd()
    os.chdir(f"{args.store}/models/")
    models = []

    # Collect model data
    for path in list_files_by_modification():
        if path.is_symlink():
            name = str(path).replace("/", "://", 1)
            file_epoch = path.lstat().st_mtime
            modified = int(time.time() - file_epoch)
            size = get_size(path)

            # Store data for later use
            models.append({"name": name, "modified": modified, "size": size})

    os.chdir(mycwd)
    return models


def info_cli(args):
    info = {
        "Engine": args.engine,
        "Image": args.image,
        "Runtime": args.runtime,
        "Store": args.store,
        "Version": version(),
    }
    print(json.dumps(info, sort_keys=True, indent=4))


def list_cli(args):
    models = _list_models(args)

    # If JSON output is requested
    if args.json:
        print(json.dumps(models))
        return

    # Calculate maximum width for each column
    name_width = len("NAME")
    modified_width = len("MODIFIED")
    size_width = len("SIZE")
    for model in models:
        modified = human_duration(model["modified"]) + " ago"
        name_width = max(name_width, len(model["name"]))
        modified_width = max(modified_width, len(modified))
        size_width = max(size_width, len(model["size"]))

    if not args.quiet and not args.noheading and not args.json:
        print(f"{'NAME':<{name_width}} {'MODIFIED':<{modified_width}} {'SIZE':<{size_width}}")

    for model in models:
        if args.quiet:
            print(model["name"])
        else:
            print(f"{model['name']:<{name_width}} {modified:<{modified_width}} {model['size']:<{size_width}}")


def help_parser(subparsers):
    parser = subparsers.add_parser("help", help="help about any command")
    # Do not run in a container
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.set_defaults(func=help_cli)


def help_cli(args):
    raise HelpException()


def pull_parser(subparsers):
    parser = subparsers.add_parser("pull", help="pull AI Model from Model registry to local storage")
    parser.add_argument("--authfile", help="path of the authentication file")
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.add_argument(
        "--tls-verify",
        dest="tlsverify",
        default=True,
        help="require HTTPS and verify certificates when contacting registries",
    )
    parser.add_argument("MODEL")  # positional argument
    parser.set_defaults(func=pull_cli)


def pull_cli(args):
    model = New(args.MODEL, args)
    matching_files = glob.glob(f"{args.store}/models/*/{model}")
    if matching_files:
        return matching_files[0]

    return model.pull(args)


def push_parser(subparsers):
    parser = subparsers.add_parser("push", help="push AI Model from local storage to remote registry")
    parser.add_argument("--authfile", help="path of the authentication file")
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.add_argument(
        "--tls-verify",
        dest="tlsverify",
        default=True,
        help="require HTTPS and verify certificates when contacting registries",
    )
    parser.add_argument("SOURCE")  # positional argument
    parser.add_argument("TARGET", nargs="?")  # positional argument
    parser.set_defaults(func=push_cli)


def _get_source(args):
    if os.path.exists(args.SOURCE):
        return args.SOURCE

    src = shortnames.resolve(args.SOURCE)
    if not src:
        src = args.SOURCE
    smodel = New(src, args)
    if smodel.type == "OCI":
        return src
    else:
        return smodel.path(args)


def push_cli(args):
    if args.TARGET:
        target = args.TARGET
        source = _get_source(args)
    else:
        target = args.SOURCE
        source = args.SOURCE

    tgt = shortnames.resolve(target)
    if not tgt:
        tgt = target

    model = New(tgt, args)
    model.push(source, args)


def run_parser(subparsers):
    parser = subparsers.add_parser("run", help="run specified AI Model as a chatbot")
    parser.add_argument("-n", "--name", dest="name", help="name of container in which the Model will be run")
    parser.add_argument("MODEL")  # positional argument
    parser.add_argument(
        "ARGS", nargs="*", help="Overrides the default prompt, and the output is returned without entering the chatbot"
    )
    parser.set_defaults(func=run_cli)


def run_cli(args):
    model = New(args.MODEL, args)
    model.run(args)


def serve_parser(subparsers):
    parser = subparsers.add_parser("serve", help="serve REST API on specified AI Model")
    parser.add_argument("-d", "--detach", action="store_true", dest="detach", help="run the container in detached mode")
    parser.add_argument("-n", "--name", dest="name", help="name of container in which the Model will be run")
    parser.add_argument("-p", "--port", default="8080", help="port for AI Model server to listen on")
    parser.add_argument(
        "--generate",
        choices=["quadlet", "kube"],
        help="generate specified configuration format for running the AI Model as a service",
    )
    parser.add_argument("MODEL")  # positional argument
    parser.set_defaults(func=serve_cli)


def serve_cli(args):
    if not args.container:
        args.detach = False
    model = New(args.MODEL, args)
    model.serve(args)


def stop_parser(subparsers):
    parser = subparsers.add_parser("stop", help="stop named container that is running AI Model")
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.add_argument("-a", "--all", action="store_true", help="stop all RamaLama containers")
    parser.add_argument(
        "--ignore", action="store_true", help="ignore errors when specified RamaLama container is missing"
    )
    parser.add_argument("NAME", nargs="?")  # positional argument
    parser.set_defaults(func=stop_container)


def _stop_container(args, name):
    if not name:
        raise IndexError("must specify a container name")
    conman = args.engine
    if conman == "":
        raise IndexError("no container manager (Podman, Docker) found")

    conman_args = [conman, "stop", "-t=0"]
    ignore_stderr = False
    if args.ignore:
        if conman == "podman":
            conman_args += ["--ignore", str(args.ignore)]
        else:
            ignore_stderr = True

    conman_args += [name]
    try:
        run_cmd(conman_args, ignore_stderr=ignore_stderr)
    except subprocess.CalledProcessError:
        if args.ignore and conman == "docker":
            return
        else:
            raise


def stop_container(args):
    if not args.all:
        return _stop_container(args, args.NAME)

    if args.NAME:
        raise IndexError("specifying --all and container name, %s, not allowed" % args.NAME)
    args.ignore = True
    args.format = "{{ .Names }}"
    for i in _list_containers(args):
        _stop_container(args, i)


def version_parser(subparsers):
    parser = subparsers.add_parser("version", help="display version of AI Model")
    # Do not run in a container
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.set_defaults(func=print_version)


def rm_parser(subparsers):
    parser = subparsers.add_parser("rm", help="remove AI Model from local storage")
    parser.add_argument("--container", default=False, action="store_false", help=argparse.SUPPRESS)
    parser.add_argument("-a", "--all", action="store_true", help="remove all local Models")
    parser.add_argument("--ignore", action="store_true", help="ignore errors when specified Model does not exist")
    parser.add_argument("MODELS", nargs="*")
    parser.set_defaults(func=rm_cli)


def _rm_model(models, args):
    for model in models:
        resolved_model = shortnames.resolve(model)
        if resolved_model:
            model = resolved_model

        model = New(model, args)
        model.remove(args)


def rm_cli(args):
    if not args.all:
        return _rm_model(args.MODELS, args)

    if len(args.MODELS) > 0:
        raise IndexError("can not specify --all as well MODEL")

    args.noheading = True
    models = [k['name'] for k in _list_models(args)]
    _rm_model(models, args)


def get_store():
    if os.geteuid() == 0:
        return "/var/lib/ramalama"

    return os.path.expanduser("~/.local/share/ramalama")


def get_gpu():
    i = 0
    gpu_num = 0
    gpu_bytes = 0
    for fp in sorted(glob.glob('/sys/bus/pci/devices/*/mem_info_vram_total')):
        with open(fp, 'r') as file:
            content = int(file.read())
            if content > 1073741824 and content > gpu_bytes:
                gpu_bytes = content
                gpu_num = i

        i += 1

    if gpu_bytes:  # this is the ROCm/AMD case
        return "HIP_VISIBLE_DEVICES", gpu_num

    return None, None


def run_container(args):
    if hasattr(args, "generate") and args.generate:
        return False

    if not args.container:
        if hasattr(args, "name") and args.name:
            raise IndexError("--nocontainer and --name options conflict. --name requires a container.")

        # --nocontainer implies --detach=false
        if hasattr(args, "detach"):
            args.detach = False
        return False

    if in_container():
        return False

    conman = args.engine
    if conman == "":
        return False

    if hasattr(args, "name") and args.name:
        name = args.name
    else:
        name = genname()

    short_file = shortnames.create_shortname_file()
    wd = find_working_directory()
    conman_args = [
        conman,
        "run",
        "--rm",
        "-i",
        "--label",
        "RAMALAMA",
        "--security-opt=label=disable",
        "-e",
        "RAMALAMA_TRANSPORT",
        "--name",
        name,
        f"-v{args.store}:/var/lib/ramalama",
        f"-v{os.path.realpath(sys.argv[0])}:/usr/bin/ramalama:ro",
        f"-v{wd}:/usr/share/ramalama/ramalama:ro",
        f"-v{short_file}:/usr/share/ramalama/shortnames.conf:ro,Z",
    ]

    di_volume = distinfo_volume()
    if di_volume != "":
        conman_args += [di_volume]

    if sys.stdout.isatty() and sys.stdin.isatty():
        conman_args += ["-t"]

    if hasattr(args, "detach") and args.detach is True:
        conman_args += ["-d"]

    if hasattr(args, "port"):
        conman_args += ["-p", f"{args.port}:{args.port}"]

    if os.path.exists("/dev/dri"):
        conman_args += ["--device", "/dev/dri"]

    if os.path.exists("/dev/kfd"):
        conman_args += ["--device", "/dev/kfd"]

    gpu_type, gpu_num = get_gpu()
    if gpu_type == "HIP_VISIBLE_DEVICES":
        conman_args += ["-e", f"{gpu_type}={gpu_num}"]
        if args.image == default_image():
            conman_args += ["quay.io/ramalama/rocm:latest"]
        else:
            conman_args += [args.image]
    else:
        conman_args += [args.image]

    conman_args += ["python3", "/usr/bin/ramalama"]
    conman_args += sys.argv[1:]
    if hasattr(args, "UNRESOLVED_MODEL"):
        index = conman_args.index(args.UNRESOLVED_MODEL)
        conman_args[index] = args.MODEL

    if args.dryrun:
        dry_run(conman_args)
        return True

    def cleanup():
        os.remove(short_file)

    atexit.register(cleanup)

    run_cmd(conman_args, stdout=None, debug=args.debug)


def dry_run(args):
    for arg in args:
        if not arg:
            continue
        if " " in arg:
            print('"%s"' % arg, end=" ")
        else:
            print("%s" % arg, end=" ")
    print()


def New(model, args):
    if model.startswith("huggingface://") or model.startswith("hf://"):
        return Huggingface(model)
    if model.startswith("ollama://"):
        return Ollama(model)
    if model.startswith("oci://") or model.startswith("docker://"):
        return OCI(model, args.engine)

    transport = os.getenv("RAMALAMA_TRANSPORT")
    if transport == "huggingface":
        return Huggingface(model)
    if transport == "ollama":
        return Ollama(model)
    if transport == "oci":
        return OCI(model, args.engine)

    return Ollama(model)


def distinfo_volume():
    dist_info = "ramalama-%s.dist-info" % version()
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), dist_info)
    if not os.path.exists(path):
        return ""

    return f"-v{path}:/usr/share/ramalama/{dist_info}:ro"
