# Copyright (C) 2026 by Fabrizio Giudici
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# VM settings: can be overridden from the command line, e.g. make start MEMORY_MB=2048
ALPINE_VERSION ?= 3.24.4
CPUS ?= 2
MEMORY_MB ?= 1024
MAC ?= 02:bb:b0:00:00:01
export ALPINE_VERSION CPUS MEMORY_MB MAC VFKIT

PREPARE := scripts/prepare-vm.sh
BRIDGEBOX := bin/BridgeBox

.PHONY: image start stop status ip ssh console clean distclean

# Downloads the Alpine cloud image (cached in cache/) and prepares the VM in target/vm.
image:
	$(PREPARE)

start: image
	$(BRIDGEBOX) start

stop:
	$(BRIDGEBOX) stop

status:
	$(BRIDGEBOX) status

ip:
	$(BRIDGEBOX) ip

ssh:
	$(BRIDGEBOX) ssh

# Follows the serial console of the VM.
console:
	tail -f target/vm/console.log

# Stops the VM and deletes it; the downloaded Alpine image is kept.
clean: stop
	rm -rf target

# Also deletes the downloaded Alpine image.
distclean: clean
	rm -rf cache
