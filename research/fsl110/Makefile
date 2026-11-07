# Out-of-tree build against the running kernel's headers.
# Not a DKMS package and not installed by the fan controller.
obj-m += fsl110.o
obj-m += fsl_ramp_kensaki.o
obj-m += fsl_probe_atarime.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
