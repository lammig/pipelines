"""
Treeshop: The Treehouse Workshop

Experimental fabric based automation to process samples on a docker-machine cluster.

NOTE: This is crafted code primarily used internal to Treehouse and assumes
quite a few things about the layout of primary and secondary files both
on a shared file server and object store. If you are not familiar with
any of these it is reccomended to stick with the Makefile for sample by sample
processing on the command line.

Storage Hierarchy:

Samples and outputs are managed on disk or S3 with the following hierarchy:

primary/
    original/
        id/
           _R1/_R2 .fastq.gz or .bam
    derived/
        id/
           _R1/_R2 .fastq.gz (only if original needs grooming)

downstream/
    id/
        secondary/
            pipeline-name-version-hash/
        tertiary/

"""
import os
import datetime
import json
import glob
from fabric.api import env, local, run, sudo, runs_once, parallel, warn_only, cd, settings
from fabric.operations import put, get

# To debug communication issues un-comment the following
# import logging
# logging.basicConfig(level=logging.DEBUG)

"""
Setup the fabric hosts environment using docker-machine ip addresses as hostnames are not
resolvable. Also point to all the per machine ssh keys. An alternative would be to use one key but
on openstack the driver deletes it on termination.
"""


def find_machines():

    """ Fill in host globals from docker-machine """
    env.user = "ubuntu"
    machines = [json.loads(open(m).read())["Driver"]
                for m in glob.glob(os.path.expanduser("~/.docker/machine/machines/*/config.json"))]
    env.hostnames = [m["MachineName"] for m in machines
                     if not env.hosts or m["MachineName"] in env.hosts]
    env.hosts = [m["IPAddress"] for m in machines
                 if not env.hosts or m["MachineName"] in env.hosts]
    # Use single key due to https://github.com/UCSC-Treehouse/pipelines/issues/5
    # env.key_filename = [m["SSHKeyPath"] for m in machines]
    env.key_filename = "~/.ssh/id_rsa"


find_machines()


@runs_once
def up(count=1):
    """ Spin up 'count' docker machines """
    print("Spinning up {} more cluster machines".format(count))
    for i in range(int(count)):
        hostname = "{}-treeshop-{:%Y%m%d-%H%M%S}".format(
            os.environ["USER"], datetime.datetime.now())
        # Create a new keypair per machine due to https://github.com/docker/machine/issues/3261
        local("""
              docker-machine create --driver openstack \
              --openstack-tenant-name treehouse \
              --openstack-auth-url http://os-con-01.pod:5000/v2.0 \
              --openstack-ssh-user ubuntu \
              --openstack-net-name treehouse-net \
              --openstack-floatingip-pool ext-net \
              --openstack-image-name Ubuntu-16.04-LTS-x86_64 \
              --openstack-flavor-name z1.medium \
              {}
              """.format(hostname))

        # Copy over single key due to https://github.com/UCSC-Treehouse/pipelines/issues/5
        local("cat ~/.ssh/id_rsa.pub" +
              "| docker-machine ssh {} 'cat >> ~/.ssh/authorized_keys'".format(hostname))

    # In case additional commands are called after up
    find_machines()


@runs_once
def down():
    """ Terminate ALL docker-machine machines """
    for host in env.hostnames:
        print("Terminating {}".format(host))
        local("docker-machine stop {}".format(host))
        local("docker-machine rm -f {}".format(host))


@runs_once
def machines():
    """ Print hostname, ip, and ssh key location of each machine """
    print("Machines:")
    for machine in zip(env.hostnames, env.hosts):
        print("{}/{}".format(machine[0], machine[1]))


def top():
    """ Get list of docker containers """
    run("docker ps")


@parallel
def configure():
    """ Copy pipeline makefile over, make directories etc... """
    sudo("gpasswd -a ubuntu docker")
    sudo("apt-get -qy install make")

    # openstack doesn't format /mnt correctly...
    sudo("umount /mnt")
    sudo("parted -s /dev/vdb mklabel gpt")
    sudo("parted -s /dev/vdb mkpart primary 2048s 100%")
    sudo("mkfs -t ext4 /dev/vdb1")
    sudo("sed -i 's/auto/ext4/' /etc/fstab")
    sudo("sed -i 's/vdb/vdb1/' /etc/fstab")
    sudo("mount /mnt")
    sudo("chmod 1777 /mnt")
    sudo("chown ubuntu:ubuntu /mnt")

    """ Downgrade docker to version supported by toil """
    run("wget https://packages.docker.com/1.12/apt/repo/pool/main/d/docker-engine/docker-engine_1.12.6~cs8-0~ubuntu-xenial_amd64.deb")  # NOQA
    sudo("apt-get -y remove docker docker-engine docker.io docker-ce")
    sudo("rm -rf /var/lib/docker")
    sudo("dpkg -i docker-engine_1.12.6~cs8-0~ubuntu-xenial_amd64.deb")

    put("{}/Makefile".format(os.path.dirname(env.real_fabfile)), "/mnt")


@parallel
def push():
    """ Update Makefile for use when iterating and debugging """
    # Copy Makefile in case we changed it while developing...
    put("{}/Makefile".format(os.path.dirname(env.real_fabfile)), "/mnt")


@parallel
def reference():
    """ Configure each machine with reference files. """
    put("{}/md5".format(os.path.dirname(env.real_fabfile)), "/mnt")
    with cd("/mnt"):
        run("REF_BASE='http://ceph-gw-01.pod/references' make reference")


def reset():
    """ Stop any existing processing and delete inputs and outputs """
    print("Resetting {}".format(env.host))
    with warn_only():
        run("docker stop $(docker ps -a -q)")
        run("docker rm $(docker ps -a -q)")
        sudo("rm -rf /mnt/samples/*")
        sudo("rm -rf /mnt/outputs/*")

        # Do we need this? Some pipeline looks like its changing it to root
        sudo("chown -R ubuntu:ubuntu /mnt")


@parallel
def process(manifest="manifest.tsv", base=".", checksum_only="False"):
    """ Process all ids listed in 'manifest' """

    def log_error(message):
        print(message)
        with open("errors.txt", "a") as error_log:
            error_log.write(message + "\n")

    # Copy Makefile in case we changed it while developing...
    put("{}/Makefile".format(os.path.dirname(env.real_fabfile)), "/mnt")

    # Read ids and pick every #hosts to allocate round robin to each machine
    with open(manifest) as f:
        ids = sorted([word.strip() for line in f.readlines() for word in line.split(',')
                      if word.strip()])[env.hosts.index(env.host)::len(env.hosts)]

    # Look for all bams and fastq's prioritizing derived over original
    samples = [{"id": id,
                "bams": [os.path.relpath(p, base) for p in sorted(
                    glob.glob("{}/primary/*/{}/*.bam".format(base, id)))],
                "fastqs": [os.path.relpath(p, base) for p in sorted(
                    glob.glob("{}/primary/*/{}/*.fastq.*".format(base, id))
                    + glob.glob("{}/primary/*/{}/*.fq.*".format(base, id)))]} for id in ids]

    print("Samples to be processed on {}:".format(env.host), samples)

    for sample in samples:
        print("{} processing {}".format(env.host, sample))

        # Reset machine clearing all output, samples, and killing dockers
        reset()

        run("mkdir -p /mnt/samples")

        if not sample["fastqs"] and not sample["bams"]:
            log_error("Unable find any fastqs or bams associated with {}".format(sample["id"]))
            continue
        elif not sample["fastqs"] and sample["bams"]:
            bam = os.path.basename(sample["bams"][0])
            print("Converting {} to fastq for {}".format(bam, sample["id"]))
            put("{}/{}".format(base, sample["bams"][0]), "/mnt/samples/")
            with cd("/mnt/samples"):
                run("docker run --rm" +
                    " -v /mnt/samples:/samples" +
                    " quay.io/ucsc_cgl/samtools@sha256:" +
                    "90528e39e246dc37421fe393795aa37fa1156d0dff59742eb243f01d2a27322e"
                    " fastq -1 /samples/{0}.R1.fastq.gz -2 /samples/{0}.R2.fastq.gz /samples/{1}"
                    .format(bam[:bam.index(".")], bam))
            local("mkdir -p {}/primary/derived/{}".format(base, sample["id"]))
            print("Copying fastqs back for archiving")
            sample["fastqs"] = get(
                "/mnt/samples/*.fastq.gz", "{}/primary/derived/{}/".format(base, sample["id"]))
            run("rm /mnt/samples/*.bam")  # Free up space
        elif len(sample["fastqs"]) < 2:
            log_error("Only found a single fastq for {}".format(sample["id"]))
            continue
        else:
            # Copy the fastqs over
            for fastq in sample["fastqs"][:2]:
                print("Copying fastq {} to cluster machine....".format(fastq))
                put("{}/{}".format(base, fastq), "/mnt/samples/")

        # Create downstream output parent
        output = "{}/downstream/{}/secondary".format(base, sample["id"])
        local("mkdir -p {}".format(output))

        # Initialize methods.json
        methods = {"user": os.environ["USER"],
                   "treeshop_version": local(
                      "git --work-tree={0} --git-dir {0}/.git describe --always".format(
                          os.path.dirname(__file__)), capture=True),
                   "sample_id": sample["id"]}

        # Calculate checksums
        methods["start"] = datetime.datetime.utcnow().isoformat()
        with settings(warn_only=True):
            result = run("cd /mnt && make checksums")
            if result.failed:
                log_error("{} Failed checksums: {}".format(sample["id"], result))
                continue

        # Update methods.json and copy output back
        dest = "{}/md5sum-3.7.0-ccba511".format(output)
        local("mkdir -p {}".format(dest))
        methods["inputs"] = sample["fastqs"]
        methods["outputs"] = [
            os.path.relpath(p, base) for p in get("/mnt/outputs/checksums/*", dest)]
        methods["end"] = datetime.datetime.utcnow().isoformat()
        methods["pipeline"] = {
            "source": "https://github.com/gliderlabs/docker-alpine",
            "docker": {
                "url": "https://hub.docker.com/alpine",
                "version": "3.7.0",
                "hash": "sha256:ccba511b1d6b5f1d83825a94f9d5b05528db456d9cf14a1ea1db892c939cda64" # NOQA
            }
        }
        with open("{}/methods.json".format(dest), "w") as f:
            f.write(json.dumps(methods, indent=4))

        if checksum_only == "True":
            continue

        # Calculate expression
        methods["start"] = datetime.datetime.utcnow().isoformat()
        with settings(warn_only=True):
            result = run("cd /mnt && make expression")
            if result.failed:
                log_error("{} Failed expression: {}".format(sample["id"], result))
                continue

        # Create output parent - wait till now in case first pipeline halted
        output = "{}/downstream/{}/secondary".format(base, sample["id"])
        local("mkdir -p {}".format(output))

        # Unpack outputs and normalize names so we don't have sample id in them
        with cd("/mnt/outputs/expression"):
            run("tar -xvf *.tar.gz --strip 1")
            run("rm *.tar.gz")
            run("mv *.sorted.bam sorted.bam")

        # Update methods.json and copy output back
        dest = "{}/ucsc_cgl-rnaseq-cgl-pipeline-3.3.4-785eee9".format(output)
        local("mkdir -p {}".format(dest))
        methods["inputs"] = sample["fastqs"]
        methods["outputs"] = [
            os.path.relpath(p, base) for p in get("/mnt/outputs/expression/*", dest)]
        methods["end"] = datetime.datetime.utcnow().isoformat()
        methods["pipeline"] = {
            "source": "https://github.com/BD2KGenomics/toil-rnaseq",
            "docker": {
                "url": "https://quay.io/ucsc_cgl/rnaseq-cgl-pipeline",
                "version": "3.3.4-1.12.3",
                "hash": "sha256:785eee9f750ab91078d84d1ee779b6f74717eafc09e49da817af6b87619b0756" # NOQA
            }
        }
        with open("{}/methods.json".format(dest), "w") as f:
            f.write(json.dumps(methods, indent=4))

        # Calculate qc (bam-umend-qc)
        methods["start"] = datetime.datetime.utcnow().isoformat()
        with settings(warn_only=True):
            result = run("cd /mnt && make qc")
            if result.failed:
                log_error("{} Failed qc: {}".format(sample["id"], result))
                continue

        # Update methods.json and copy output back
        dest = "{}/ucsctreehouse-bam-umend-qc-1.1.0-cc481e4".format(output)
        local("mkdir -p {}".format(dest))
        methods["inputs"] = ["{}/ucsc_cgl-rnaseq-cgl-pipeline-3.3.4-785eee9/sorted.bam".format(
                os.path.relpath(output, base))]
        methods["outputs"] = [
            os.path.relpath(p, base) for p in get("/mnt/outputs/qc/*", dest)]
        methods["end"] = datetime.datetime.utcnow().isoformat()
        methods["pipeline"] = {
            "source": "https://github.com/UCSC-Treehouse/bam-umend-qc",
            "docker": {
                "url": "https://hub.docker.com/r/ucsctreehouse/bam-umend-qc",
                "version": "1.1.0",
                "hash": "sha256:cc481e413735e36b96caaa7fff977e591983e08eb5a625fed3aa90dd7108817e" # NOQA
            }
        }
        with open("{}/methods.json".format(dest), "w") as f:
            f.write(json.dumps(methods, indent=4))

        # Calculate fusion
        methods["start"] = datetime.datetime.utcnow().isoformat()
        with settings(warn_only=True):
            result = run("cd /mnt && make fusions")
            if result.failed:
                log_error("{} Failed fusions: {}".format(sample["id"], result))
                continue

        # Update methods.json and copy output back
        dest = "{}/ucsctreehouse-fusion-0.1.0-3faac56".format(output)
        local("mkdir -p {}".format(dest))
        methods["inputs"] = sample["fastqs"]
        methods["outputs"] = [
            os.path.relpath(p, base) for p in get("/mnt/outputs/fusions/*", dest)]
        methods["end"] = datetime.datetime.utcnow().isoformat()
        methods["pipeline"] = {
            "source": "https://github.com/UCSC-Treehouse/fusion",
            "docker": {
                "url": "https://hub.docker.com/r/ucsctreehouse/fusion",
                "version": "0.1.0",
                "hash": "sha256:3faac562666363fa4a80303943a8f5c14854a5f458676e1248a956c13fb534fd" # NOQA
            }
        }
        with open("{}/methods.json".format(dest), "w") as f:
            f.write(json.dumps(methods, indent=4))

        # Calculate variants
        methods["start"] = datetime.datetime.utcnow().isoformat()
        with settings(warn_only=True):
            result = run("cd /mnt && make variants")
            if result.failed:
                log_error("{} Failed variants: {}".format(sample["id"], result))
                continue

        # Update methods.json and copy output back
        dest = "{}/ucsctreehouse-mini-var-call-0.0.1-1976429".format(output)
        local("mkdir -p {}".format(dest))
        methods["inputs"] = ["{}/ucsc_cgl-rnaseq-cgl-pipeline-3.3.4-785eee9/sorted.bam".format(
                os.path.relpath(output, base))]
        methods["outputs"] = [
            os.path.relpath(p, base) for p in get("/mnt/outputs/variants/*", dest)]
        methods["end"] = datetime.datetime.utcnow().isoformat()
        methods["pipeline"] = {
            "source": "https://github.com/UCSC-Treehouse/mini-var-call",
            "docker": {
                "url": "https://hub.docker.com/r/ucsctreehouse/mini-var-call",
                "version": "0.0.1",
                "hash": "sha256:197642937956ae73465ad2ef4b42501681ffc3ef07fecb703f58a3487eab37ff" # NOQA
            }
        }
        with open("{}/methods.json".format(dest), "w") as f:
            f.write(json.dumps(methods, indent=4))
