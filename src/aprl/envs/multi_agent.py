import collections

from baselines.common.vec_env import VecEnv, VecEnvWrapper
from baselines.common.vec_env.dummy_vec_env import DummyVecEnv
from baselines.common.vec_env.subproc_vec_env import SubprocVecEnv
import gym
from gym import Env, Wrapper
import numpy as np

from aprl.utils import getattr_unwrapped


class MultiAgentEnv(Env):
    """Abstract class for multi-agent environments.
       This differs from the normal gym.Env in two ways:
         + step returns a tuple of observations, each a numpy array, and a tuple of rewards.
         + It has an additional attribute num_agents.
       Moreover, we guarantee that observation_space and action_space are a Tuple, with the
       i'th element corresponding to the i'th agents observation and action space.

       This should really be a different class since it is-not a gym.Env,
       however it's very convenient to have it interoperate with the rest of the
       Gym infrastructure, so we'll abuse this. Sadly there is still no standard
       for multi-agent environments in Gym, issue #934 is working on it."""
    def __init__(self, num_agents):
        self.num_agents = num_agents
        assert len(self.action_space.spaces) == num_agents
        assert len(self.observation_space.spaces) == num_agents

    def step(self, action_n):
        """Run one timestep of the environment's dynamics.
           Accepts an action_n consisting of a self.num_agents length list.

           :param action_n (list<ndarray>): actions per agent.
           :return a tuple containing:
                obs_n (tuple<ndarray>): observations per agent.
                reward_n (tuple<float>): reward per agent.
                done (bool): episode over.
                info (dict): auxiliary diagnostic info."""
        raise NotImplementedError

    def reset(self):
        """Resets state of environment.
        :return: observation (list<ndarray>): per agent."""
        raise NotImplementedError


class MultiWrapper(Wrapper, MultiAgentEnv):
    def __init__(self, env):
        Wrapper.__init__(self, env)
        MultiAgentEnv.__init__(self, getattr_unwrapped(env, 'num_agents'))


class FakeSingleSpaces(gym.Env):
    """Creates a fake gym.Env that has action and observation spaces corresponding to
       those of agent_id in a MultiEnv env. This is useful for functions that construct
       policy or reward networks given an environment. It will throw an error if reset,
       step or other methods are called."""
    def __init__(self, env, agent_id=0):
        self.observation_space = env.observation_space.spaces[agent_id]
        self.action_space = env.action_space.spaces[agent_id]


class FakeSingleSpacesVec(VecEnv):
    """VecEnv equivalent of FakeSingleSpaces."""
    def __init__(self, venv, agent_id=0):
        observation_space = venv.observation_space.spaces[agent_id]
        action_space = venv.action_space.spaces[agent_id]
        super().__init__(venv.num_envs, observation_space, action_space)

    def reset(self):
        raise NotImplementedError()

    def step_async(self, actions):
        raise NotImplementedError()

    def step_wait(self):
        raise NotImplementedError()

    def close_extras(self):
        raise NotImplementedError()


class FlattenSingletonEnv(Wrapper):
    """Adapts a single-agent MultiAgentEnv into a standard Gym Env.

    This is typically used after first applying CurryEnv until there is only one agent left."""
    def __init__(self, env):
        '''
        :param env: a MultiAgentEnv.
        :return a single-agent Gym environment.
        '''
        assert env.num_agents == 1
        super().__init__(env)
        self.observation_space = env.observation_space.spaces[0]
        self.action_space = env.action_space.spaces[0]

    def step(self, action):
        observations, rewards, done, infos = self.env.step([action])
        return observations[0], rewards[0], done, infos

    def reset(self):
        return self.env.reset()[0]


def flatten_space(tuple_space):
    """Flattens a Tuple of like-spaces into a single bigger space of the appropriate type.
       The spaces do not have to have the same shape, but do need to be of compatible types.
       For example, we can flatten a (Box(10), Box(5)) into Box(15) or a (Discrete(2), Discrete(2))
       into a MultiDiscrete([2, 2]), but cannot flatten a (Box(10), Discrete(2))."""
    unique_types = set(type(space) for space in tuple_space.spaces)
    if len(unique_types) > 1:
        raise TypeError(f"Cannot flatten a space with more than one type: {unique_types}")
    type = unique_types.pop()

    if isinstance(type, gym.spaces.Discrete):
        flat_space = gym.spaces.MultiDiscrete([space.n for space in tuple_space.spaces])
        flatten = unflatten = lambda x: x
    elif isinstance(type, gym.spaces.MultiDiscrete):
        flat_space = gym.spaces.MultiDiscrete([space.nvec for space in tuple_space.spaces])
        flatten = unflatten = lambda x: x
    elif isinstance(type, gym.spaces.Box):
        low = np.concatenate(*[space.low for space in tuple_space.spaces], axis=0)
        high = np.concatenate(*[space.high for space in tuple_space.spaces], axis=0)
        flat_space = gym.spaces.Box(low=low, high=high)

        def flatten(x):
            return np.flatten(x)

        def unflatten(x):
            sizes = [np.prod(space.shape) for space in tuple_space.spaces]
            start = np.cumsum(sizes)
            end = start[1:] + len(x)
            orig = [np.reshape(x[s:e], space.shape)
                    for s, e, space in zip(start, end, tuple_space.spaces)]
            return orig
    else:
        raise NotImplementedError("Unsupported type: f{type}")
    return flat_space, flatten, unflatten


class FlattenMultiEnv(Wrapper):
    """Adapts a MultiAgentEnv into a standard Gym Env by flattening actions and observations.

    This can be used if you wish to perform centralized training and execution
    in a multi-agent RL environment."""
    def __init__(self, env, reward_agg=sum):
        '''
        :param env(MultiAgentEnv): a MultiAgentEnv with any number of agents.
        :param reward_agg(list<float>->float): a function reducing a list of rewards.
        :return a single-agent Gym environment.
        '''
        self.observation_space, self._obs_flatten, _ = flatten_space(env.observation_space)
        self.action_space, _, self._act_unflatten = flatten_space(env.action_space)
        self.reward_agg = reward_agg
        super().__init__(env)

    def step(self, action):
        action = self._act_unflatten(action)
        observations, rewards, done, infos = self.env.step(action)
        return self._obs_flatten(observations), self.reward_agg(rewards), done, infos

    def reset(self):
        return self.env.reset()[0]


def _tuple_pop(input, i):
    output = list(input)
    elt = output.pop(i)
    return tuple(output), elt


def _tuple_space_filter(tuple_space, filter_idx):
    filtered_spaces = (space for i, space in enumerate(tuple_space.spaces) if i != filter_idx)
    return gym.spaces.Tuple(tuple(filtered_spaces))


class CurryEnv(MultiWrapper):
    """Substitutes in a fixed agent for one of the players in a MultiAgentEnv."""
    def __init__(self, env, agent, agent_idx=0):
        """Fixes one of the players in a MultiAgentEnv.
        :param env(MultiAgentEnv): the environment.
        :param agent(ResettableAgent): The agent to be fixed
        :param agent_idx(int): The index of the agent that should be fixed
        :return: a new MultiAgentEnv with num_agents decremented. It behaves like env but
                 with all actions at index agent_idx set to those returned by agent.
        """
        super().__init__(env)

        assert env.num_agents >= 1  # allow currying the last agent
        self.num_agents = env.num_agents - 1
        self.observation_space = _tuple_space_filter(self.observation_space, agent_idx)
        self.action_space = _tuple_space_filter(self.action_space, agent_idx)

        self._agent_to_fix = agent_idx
        self._agent = agent
        self._last_obs = None
        self._last_reward = None

    def step(self, actions):
        action = self._agent.get_action(self._last_obs)
        actions.insert(self._agent_to_fix, action)
        observations, rewards, done, infos = self.env.step(actions)

        observations, self._last_obs = _tuple_pop(observations, self._agent_to_fix)
        rewards, self._last_reward = _tuple_pop(rewards, self._agent_to_fix)

        return observations, rewards, done, infos

    def reset(self):
        observations = self.env.reset()
        observations, self._last_obs = _tuple_pop(observations, self._agent_to_fix)
        return observations


class VecMultiEnv(VecEnv):
    """Like a VecEnv, but each environment is a MultiEnv. Adds extra attribute, num_agents.

       Observations and actions are a num_agents-length tuple, with the i'th entry of shape
       (num_envs, ) + {observation,action}_space.spaces[i].shape. Rewards are a ndarray of shape
       (num_agents, num_envs)."""
    def __init__(self, num_envs, num_agents, observation_space, action_space):
        VecEnv.__init__(self, num_envs, observation_space, action_space)
        self.num_agents = num_agents


class VecMultiWrapper(VecEnvWrapper, VecMultiEnv):
    """Like VecEnvWrapper but for VecMultiEnv's."""
    def __init__(self, venv):
        VecEnvWrapper.__init__(self, venv)
        VecMultiEnv.__init__(self, venv.num_envs, venv.num_agents,
                             venv.observation_space, venv.action_space)


def _tuple_to_dict(tuple):
    return collections.OrderedDict(((i, v) for i, v in enumerate(tuple)))


def _tuple_to_dict_space(tuple_space):
    return gym.spaces.Dict(_tuple_to_dict(tuple_space.spaces))


def _dict_to_tuple(d):
    min_k = min(d.keys())
    assert min_k == 0
    max_k = max(d.keys())
    return tuple(d[i] for i in range(max_k + 1))


def _dict_to_tuple_space(dict_space):
    return gym.spaces.Tuple(_dict_to_tuple(dict_space.spaces))


class _TupleToDict(MultiWrapper):
    def __init__(self, env):
        super().__init__(env)
        self.action_space = _tuple_to_dict_space(env.action_space)
        self.observation_space = _tuple_to_dict_space(env.observation_space)

    def step(self, action):
        obs, rews, done, info = self.env.step(action)
        obs = _tuple_to_dict(obs)
        return obs, rews, done, info

    def reset(self):
        obs = self.env.reset()
        return _tuple_to_dict(obs)


def _decorate_tuple_to_dict(env_fn):
    def f():
        env = env_fn()
        return _TupleToDict(env)
    return f


def tuple_transpose(xs):
    '''Permutes environment and agent dimension.

    Specifically, VecMultiEnv has an agent-major convention: actions and observations are
    num_agents-length tuples, with the i'th element a num_env-length tuple containing an
    agent & environment specific action/observation. This convention is convenient since we can
    easily mutex the stream to different agents.

    However, it can also be convenient to have an environment-major convention: that is, there is
    a num_envs-length tuple each containing a num_agents-length tuple. In particular, this is the
    most natural internal representation for VecEnv, and is also convenient when sampling from
    the action or observation space of an environment.
    '''
    inner_len = len(xs[0])
    for x in xs:
        assert len(x) == inner_len
    return tuple(tuple([x[i] for x in xs]) for i in range(inner_len))


class _DictToTuple(VecMultiWrapper):
    def __init__(self, venv):
        super().__init__(venv)
        self.action_space = _dict_to_tuple_space(venv.action_space)
        self.observation_space = _dict_to_tuple_space(venv.observation_space)

    def reset(self):
        obs = self.venv.reset()
        return _dict_to_tuple(obs)

    def step_async(self, actions):
        actions_per_env = tuple_transpose(actions)
        return self.venv.step_async(actions_per_env)

    def step_wait(self):
        obs, rews, done, info = self.venv.step_wait()
        obs = _dict_to_tuple(obs)
        rews = rews.T
        return obs, rews, done, info


def _make_vec_multi_env(cls):
    def f(env_fns):
        env_fns = [_decorate_tuple_to_dict(fn) for fn in env_fns]
        venv = cls(env_fns)
        return _DictToTuple(venv)
    return f


class _DummyVecMultiEnv(DummyVecEnv, VecMultiEnv):
    """Like DummyVecEnv but implements VecMultiEnv interface.
       Handles the larger reward size.
       Note SubprocVecEnv works out of the box."""
    def __init__(self, env_fns):
        DummyVecEnv.__init__(self, env_fns)
        num_agents = getattr_unwrapped(self.envs[0], 'num_agents')
        VecMultiEnv.__init__(self, self.num_envs, num_agents,
                             self.observation_space, self.action_space)
        self.buf_rews = np.zeros((self.num_envs, self.num_agents), dtype=np.float32)


class _SubprocVecMultiEnv(SubprocVecEnv, VecMultiEnv):
    """Stand-in for SubprocVecEnv when applied to MultiEnv's."""
    def __init__(self, env_fns):
        SubprocVecEnv.__init__(self, env_fns)
        env = env_fns[0]()
        num_agents = getattr_unwrapped(env, 'num_agents')
        env.close()
        VecMultiEnv.__init__(self, self.num_envs, num_agents,
                             self.observation_space, self.action_space)


# TODO: This code is extremely hacky. The best approach is probably to add native support for
# tuples to {Dummy,Subproc}VecEnv. Adding support for handling dict action spaces would be an
# easier alternative and avoid some special casing now. See baselines issue #555.
make_dummy_vec_multi_env = _make_vec_multi_env(_DummyVecMultiEnv)
make_subproc_vec_multi_env = _make_vec_multi_env(_SubprocVecMultiEnv)