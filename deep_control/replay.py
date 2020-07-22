import numpy as np
import torch


def unique(sorted_array):
    """
    More efficient implementation of np.unique for sorted arrays
    :param sorted_array: (np.ndarray)
    :return:(np.ndarray) sorted_array without duplicate elements
    """
    if len(sorted_array) == 1:
        return sorted_array
    left = sorted_array[:-1]
    right = sorted_array[1:]
    uniques = np.append(right != left, True)
    return sorted_array[uniques]


class SegmentTree:
    def __init__(self, capacity, operation, neutral_element):
        """
        Build a Segment Tree data structure.
        https://en.wikipedia.org/wiki/Segment_tree
        Can be used as regular array that supports Index arrays, but with two
        important differences:
            a) setting item's value is slightly slower.
               It is O(lg capacity) instead of O(1).
            b) user has access to an efficient ( O(log segment size) )
               `reduce` operation which reduces `operation` over
               a contiguous subsequence of items in the array.
        :param capacity: (int) Total size of the array - must be a power of two.
        :param operation: (lambda (Any, Any): Any) operation for combining elements (eg. sum, max) must form a
            mathematical group together with the set of possible values for array elements (i.e. be associative)
        :param neutral_element: (Any) neutral element for the operation above. eg. float('-inf') for max and 0 for sum.
        """
        assert (
            capacity > 0 and capacity & (capacity - 1) == 0
        ), "capacity must be positive and a power of 2."
        self._capacity = capacity
        self._value = [neutral_element for _ in range(2 * capacity)]
        self._operation = operation
        self.neutral_element = neutral_element

    def _reduce_helper(self, start, end, node, node_start, node_end):
        if start == node_start and end == node_end:
            return self._value[node]
        mid = (node_start + node_end) // 2
        if end <= mid:
            return self._reduce_helper(start, end, 2 * node, node_start, mid)
        else:
            if mid + 1 <= start:
                return self._reduce_helper(start, end, 2 * node + 1, mid + 1, node_end)
            else:
                return self._operation(
                    self._reduce_helper(start, mid, 2 * node, node_start, mid),
                    self._reduce_helper(mid + 1, end, 2 * node + 1, mid + 1, node_end),
                )

    def reduce(self, start=0, end=None):
        """
        Returns result of applying `self.operation`
        to a contiguous subsequence of the array.
            self.operation(arr[start], operation(arr[start+1], operation(... arr[end])))
        :param start: (int) beginning of the subsequence
        :param end: (int) end of the subsequences
        :return: (Any) result of reducing self.operation over the specified range of array elements.
        """
        if end is None:
            end = self._capacity
        if end < 0:
            end += self._capacity
        end -= 1
        return self._reduce_helper(start, end, 1, 0, self._capacity - 1)

    def __setitem__(self, idx, val):
        # indexes of the leaf
        idxs = idx + self._capacity
        self._value[idxs] = val
        if isinstance(idxs, int):
            idxs = np.array([idxs])
        # go up one level in the tree and remove duplicate indexes
        idxs = unique(idxs // 2)
        while len(idxs) > 1 or idxs[0] > 0:
            # as long as there are non-zero indexes, update the corresponding values
            self._value[idxs] = self._operation(
                self._value[2 * idxs], self._value[2 * idxs + 1]
            )
            # go up one level in the tree and remove duplicate indexes
            idxs = unique(idxs // 2)

    def __getitem__(self, idx):
        assert np.max(idx) < self._capacity
        assert 0 <= np.min(idx)
        return self._value[self._capacity + idx]


class SumSegmentTree(SegmentTree):
    def __init__(self, capacity):
        super(SumSegmentTree, self).__init__(
            capacity=capacity, operation=np.add, neutral_element=0.0
        )
        self._value = np.array(self._value)

    def sum(self, start=0, end=None):
        """
        Returns arr[start] + ... + arr[end]
        :param start: (int) start position of the reduction (must be >= 0)
        :param end: (int) end position of the reduction (must be < len(arr), can be None for len(arr) - 1)
        :return: (Any) reduction of SumSegmentTree
        """
        return super(SumSegmentTree, self).reduce(start, end)

    def find_prefixsum_idx(self, prefixsum):
        """
        Find the highest index `i` in the array such that
            sum(arr[0] + arr[1] + ... + arr[i - i]) <= prefixsum for each entry in prefixsum
        if array values are probabilities, this function
        allows to sample indexes according to the discrete
        probability efficiently.
        :param prefixsum: (np.ndarray) float upper bounds on the sum of array prefix
        :return: (np.ndarray) highest indexes satisfying the prefixsum constraint
        """
        if isinstance(prefixsum, float):
            prefixsum = np.array([prefixsum])
        assert 0 <= np.min(prefixsum)
        assert np.max(prefixsum) <= self.sum() + 1e-5
        assert isinstance(prefixsum[0], float)

        idx = np.ones(len(prefixsum), dtype=int)
        cont = np.ones(len(prefixsum), dtype=bool)

        while np.any(cont):  # while not all nodes are leafs
            idx[cont] = 2 * idx[cont]
            prefixsum_new = np.where(
                self._value[idx] <= prefixsum, prefixsum - self._value[idx], prefixsum
            )
            # prepare update of prefixsum for all right children
            idx = np.where(
                np.logical_or(self._value[idx] > prefixsum, np.logical_not(cont)),
                idx,
                idx + 1,
            )
            # Select child node for non-leaf nodes
            prefixsum = prefixsum_new
            # update prefixsum
            cont = idx < self._capacity
            # collect leafs
        return idx - self._capacity


class MinSegmentTree(SegmentTree):
    def __init__(self, capacity):
        super(MinSegmentTree, self).__init__(
            capacity=capacity, operation=np.minimum, neutral_element=float("inf")
        )
        self._value = np.array(self._value)

    def min(self, start=0, end=None):
        """
        Returns min(arr[start], ...,  arr[end])
        :param start: (int) start position of the reduction (must be >= 0)
        :param end: (int) end position of the reduction (must be < len(arr), can be None for len(arr) - 1)
        :return: (Any) reduction of MinSegmentTree
        """
        return super(MinSegmentTree, self).reduce(start, end)


class ReplayBufferStorage:
    def __init__(self, size, obs_shape, action_shape):
        self.s_stack = torch.zeros((size,) + obs_shape, dtype=torch.float32)
        self.action_stack = torch.zeros((size,) + action_shape, dtype=torch.float32)
        self.reward_stack = torch.zeros((size, 1), dtype=torch.float32)
        self.s1_stack = torch.zeros((size,) + obs_shape, dtype=torch.float32)
        self.done_stack = torch.zeros((size, 1), dtype=torch.int)

        self.size = size
        self._next_idx = 0
        self._max_filled = 0

    def __len__(self):
        return self._max_filled

    def add(self, s, a, r, s_1, d):
        # this buffer supports batched experience
        if len(s.shape) > 1:
            num_samples = len(s)
        else:
            num_samples = 1

        # convert to torch tensors
        s = torch.from_numpy(s).float()
        a = torch.from_numpy(a).float()
        r = torch.Tensor([r]).float()
        s_1 = torch.from_numpy(s_1).float()
        d = torch.Tensor([d]).int()

        # Store at end of buffer. Wrap around if past end.
        R = np.arange(self._next_idx, self._next_idx + num_samples) % self.size
        self.s_stack[R] = s
        self.action_stack[R] = a
        self.reward_stack[R] = r
        self.s1_stack[R] = s_1
        self.done_stack[R] = d
        # Advance index.
        self._max_filled = min(
            max(self._next_idx + num_samples, self._max_filled), self.size
        )
        self._next_idx = (self._next_idx + num_samples) % self.size
        return R

    def __getitem__(self, indices):
        try:
            iter(indices)
        except ValueError:
            raise IndexError(
                "ReplayBufferStorage getitem called with indices object that is not iterable"
            )
        state = self.s_stack[indices]
        action = self.action_stack[indices]
        reward = self.reward_stack[indices]
        next_state = self.s1_stack[indices]
        done = self.done_stack[indices]
        return (state, action, reward, next_state, done)

    def get_all_transitions(self):
        return (
            self.s_stack[: self._max_filled],
            self.action_stack[: self._max_filled],
            self.reward_stack[: self._max_filled],
            self.s1_stack[: self._max_filled],
            self.done_stack[: self._max_filled],
        )


class ReplayBuffer:
    def __init__(self, size):
        self._storage = None
        self._maxsize = size

    def __len__(self):
        return len(self._storage)

    def push(self, state, action, reward, next_state, done):
        if not self._storage:
            self._storage = ReplayBufferStorage(
                self._maxsize, obs_shape=state.shape, action_shape=action.shape,
            )
        return self._storage.add(state, action, reward, next_state, done)

    def sample(self, batch_size):
        random_idxs = torch.randint(len(self._storage), (batch_size,))
        return self._storage[random_idxs]

    def get_all_transitions(self):
        return self._storage.get_all_transitions()


class PrioritizedReplayBuffer(ReplayBuffer):
    def __init__(self, size, alpha=0.6, beta=1.0):
        super(PrioritizedReplayBuffer, self).__init__(size)
        assert alpha >= 0
        self.alpha = alpha
        self.beta = beta

        it_capacity = 1
        while it_capacity < size:
            it_capacity *= 2

        self._it_sum = SumSegmentTree(it_capacity)
        self._it_min = MinSegmentTree(it_capacity)
        self._max_priority = 1.0

    def push(self, s, a, r, s_1, d, priorities=None):
        R = super().push(s, a, r, s_1, d)
        if not priorities:
            priorities = self._max_priority
        self._it_sum[R] = priorities ** self.alpha
        self._it_min[R] = priorities ** self.alpha

    def _sample_proportional(self, batch_size):
        mass = []
        total = self._it_sum.sum(0, len(self._storage) - 1)
        mass = np.random.random(size=batch_size) * total
        idx = self._it_sum.find_prefixsum_idx(mass)
        return idx

    def sample(self, batch_size):
        idxes = self._sample_proportional(batch_size)
        p_min = self._it_min.min() / self._it_sum.sum()
        max_weight = (p_min * len(self._storage)) ** (-self.beta)
        p_sample = self._it_sum[idxes] / self._it_sum.sum()
        weights = (p_sample * len(self._storage)) ** (-self.beta) / max_weight
        return self._storage[idxes], torch.from_numpy(weights), idxes

    def update_priorities(self, idxes, priorities):
        assert len(idxes) == len(priorities)
        assert np.min(priorities) > 0
        assert np.min(idxes) >= 0
        assert np.max(idxes) < len(self._storage)
        self._it_sum[idxes] = priorities ** self.alpha
        self._it_min[idxes] = priorities ** self.alpha
        self._max_priority = max(self._max_priority, np.max(priorities))