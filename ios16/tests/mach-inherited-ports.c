#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

enum {
	TASK_PORT_REGISTER_MAX = 3,
	KERN_SUCCESS = 0,
	KERN_INVALID_ARGUMENT = 4,
	KERN_RESOURCE_SHORTAGE = 6,
	KERN_INVALID_RIGHT = 17,
	Z_WAITOK = 1,
	Z_ZERO = 2,
};

typedef int kern_return_t;
typedef unsigned mach_msg_type_number_t;
struct port {
	bool ip_immovable_send;
	unsigned sends;
};
typedef struct port *ipc_port_t;
typedef ipc_port_t mach_port_t;
typedef struct {
	mach_port_t port;
} mach_port_ool_t;
typedef mach_port_ool_t *mach_port_array_t;
struct task {
	bool ipc_active;
	bool locked;
	ipc_port_t itk_registered[TASK_PORT_REGISTER_MAX];
};
typedef struct task *task_t;
#define TASK_NULL NULL
#define IP_NULL NULL
#define IP_VALID(port) ((port) != IP_NULL)

static unsigned live_allocations;
static bool fail_allocation;

static void *
allocate(size_t count, size_t size, unsigned flags)
{
	assert(flags == (Z_WAITOK | Z_ZERO));
	if (fail_allocation) {
		return NULL;
	}
	void *bytes = calloc(count, size);
	assert(bytes != NULL);
	++live_allocations;
	return bytes;
}

static void
deallocate(void *bytes, size_t count)
{
	assert(bytes != NULL && live_allocations > 0 && count > 0);
	--live_allocations;
	free(bytes);
}

#define mach_port_array_alloc(count, flags) allocate(count, sizeof(mach_port_ool_t), flags)
#define mach_port_array_free(bytes, count) deallocate(bytes, count)

static void itk_lock(task_t task) { assert(!task->locked); task->locked = true; }
static void itk_unlock(task_t task) { assert(task->locked); task->locked = false; }

static void
ipc_port_release_send(ipc_port_t port)
{
	if (port != IP_NULL) {
		assert(port->sends > 0);
		--port->sends;
	}
}

static ipc_port_t
ipc_port_copy_send_any(ipc_port_t port)
{
	if (port != IP_NULL) {
		assert(port->sends > 0);
		++port->sends;
	}
	return port;
}

/* XNU_IMPLEMENTATIONS */

int
main(void)
{
	struct task task = { .ipc_active = true };
	struct task inactive = { 0 };
	struct port old = { .sends = 1 };
	struct port good = { .sends = 1 };
	struct port immovable = { .ip_immovable_send = true, .sends = 1 };
	task.itk_registered[2] = &old;

	/* Registration transfers the supplied right, clears unused slots and frees the array. */
	mach_port_array_t request = mach_port_array_alloc(1, Z_WAITOK | Z_ZERO);
	request[0].port = &good;
	assert(mach_ports_register(&task, request, 1) == KERN_SUCCESS);
	assert(live_allocations == 0 && good.sends == 1 && old.sends == 0);
	assert(task.itk_registered[0] == &good && task.itk_registered[1] == IP_NULL);
	assert(task.itk_registered[2] == IP_NULL);

	/* Lookup returns its own array and a duplicated send reference. */
	mach_port_array_t reply = NULL;
	mach_msg_type_number_t count = 0;
	assert(mach_ports_lookup(&task, &reply, &count) == KERN_SUCCESS);
	assert(reply != NULL && count == TASK_PORT_REGISTER_MAX && good.sends == 2);
	assert(reply[0].port == &good && reply[1].port == IP_NULL && reply[2].port == IP_NULL);
	ipc_port_release_send(reply[0].port);
	mach_port_array_free(reply, count);

	/* A failed call leaves the incoming array and rights with MIG for cleanup. */
	request = mach_port_array_alloc(1, Z_WAITOK | Z_ZERO);
	request[0].port = &immovable;
	assert(mach_ports_register(&task, request, 1) == KERN_INVALID_RIGHT);
	assert(mach_ports_register(&task, request, TASK_PORT_REGISTER_MAX + 1) == KERN_INVALID_ARGUMENT);
	assert(mach_ports_register(&task, NULL, 1) == KERN_INVALID_ARGUMENT);
	assert(mach_ports_register(TASK_NULL, request, 1) == KERN_INVALID_ARGUMENT);
	request[0].port = &good;
	assert(mach_ports_register(&inactive, request, 1) == KERN_INVALID_ARGUMENT);
	assert(task.itk_registered[0] == &good && immovable.sends == 1 && live_allocations == 1);
	mach_port_array_free(request, 1);

	reply = NULL;
	count = 99;
	assert(mach_ports_lookup(&inactive, &reply, &count) == KERN_INVALID_ARGUMENT);
	assert(mach_ports_lookup(TASK_NULL, &reply, &count) == KERN_INVALID_ARGUMENT);
	fail_allocation = true;
	assert(mach_ports_lookup(&task, &reply, &count) == KERN_RESOURCE_SHORTAGE);
	assert(reply == NULL && count == 99 && live_allocations == 0);
	assert(mach_ports_register(&task, NULL, 0) == KERN_SUCCESS);
	assert(task.itk_registered[0] == IP_NULL && good.sends == 0);
	puts("Legacy Mach RPC checks passed: registration, lookup, ownership, and failure paths.");
	return 0;
}
