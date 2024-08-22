#!/usr/bin/python3

import errno
import os
from pathlib import Path
import subprocess
import ramalama
import sys


def main(args):
    try:
        conman = ramalama.container_manager()
        store = ramalama.create_store()

        dryrun = False
        while len(args) > 0:
            if args[0] == "--dryrun":
                args.pop(0)
                dryrun = True
            elif args[0] in ramalama.funcDict:
                break
            else:
                ramalama.perror(f"Error: unrecognized command `{args[0]}`\n")
                ramalama.usage()

        port = "8080"
        host = os.getenv('RAMALAMA_HOST', port)
        if host != port:
            port = host.rsplit(':', 1)[1]

        if conman:
            home = os.path.expanduser('~')
            cwd = os.getcwd()
            wd = os.path.join(cwd, "ramalama")
            if not os.path.exists(wd):
                wd = "/usr/lib/python3.12/site-packages/podman"
            libpath = "/usr/lib/python3.12/site-packages/ramalama"
            conman_args = [conman, "run",
                           "--rm",
                           "-it",
                           "--security-opt=label=disable",
                           f"-v{store}:/var/lib/ramalama",
                           f"-v{home}:{home}",
                           "-v/tmp:/tmp",
                           f"-v{sys.argv[0]}:/usr/bin/ramalama",
                           f"-v{wd}:{libpath}",
                           "-e", "RAMALAMA_HOST",
                           "-p", f"{host}:{port}",
                           "quay.io/ramalama/ramalama:latest", __file__] + args
            if dryrun:
                return print(*conman_args)

            ramalama.exec_cmd(conman_args)

        cmd = args.pop(0)
        ramalama.funcDict[cmd](store, args, port)
    except IndexError as e:
        ramalama.perror(str(e).strip("'"))
        sys.exit(errno.EINVAL)
    except KeyError as e:
        ramalama.perror(str(e).strip("'"))
        sys.exit(1)
    except NotImplementedError as e:
        ramalama.perror(str(e).strip("'"))
        sys.exit(errno.ENOTSUP)
    except subprocess.CalledProcessError as e:
        ramalama.perror(str(e).strip("'"))
        sys.exit(e.returncode)


if __name__ == "__main__":
    main(sys.argv[1:])