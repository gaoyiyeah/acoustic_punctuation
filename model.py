
from theano import tensor
from toolz import merge

from blocks.bricks import (Tanh, Maxout, Linear, FeedforwardSequence,
                           Bias, Initializable, MLP)
from blocks.bricks.attention import SequenceContentAttention
from blocks.bricks.base import application
from blocks.bricks.lookup import LookupTable
from blocks.bricks.parallel import Fork
from blocks.bricks.recurrent import GatedRecurrent, Bidirectional
from blocks.bricks.sequence_generators import (
    LookupFeedback, Readout, SoftmaxEmitter,
    SequenceGenerator)
from blocks.roles import add_role, WEIGHT
from blocks.utils import shared_floatx_nans

from picklable_itertools.extras import equizip


# Helper class
class InitializableFeedforwardSequence(FeedforwardSequence, Initializable):
    pass


class LookupFeedbackWMT15(LookupFeedback):
    """Zero-out initial readout feedback by checking its value."""

    @application
    def feedback(self, outputs):
        assert self.output_dim == 0

        shp = [outputs.shape[i] for i in range(outputs.ndim)]
        outputs_flat = outputs.flatten()
        outputs_flat_zeros = tensor.switch(outputs_flat < 0, 0,
                                           outputs_flat)

        lookup_flat = tensor.switch(
            outputs_flat[:, None] < 0,
            tensor.alloc(0., outputs_flat.shape[0], self.feedback_dim),
            self.lookup.apply(outputs_flat_zeros))
        lookup = lookup_flat.reshape(shp+[self.feedback_dim])
        return lookup


class BidirectionalWMT15(Bidirectional):
    """Wrap two Gated Recurrents each having separate parameters."""

    @application
    def apply(self, forward_dict, backward_dict):
        """Applies forward and backward networks and concatenates outputs."""
        forward = self.children[0].apply(as_list=True, **forward_dict)
        backward = [x[::-1] for x in self.children[1].apply(reverse=True, as_list=True, **backward_dict)]
        return [tensor.concatenate([f, b], axis=2) for f, b in equizip(forward, backward)]


class BidirectionalEncoder(Initializable):
    """Encoder of RNNsearch model."""

    def __init__(self, vocab_size, embedding_dim, state_dim, **kwargs):
        super(BidirectionalEncoder, self).__init__(**kwargs)
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.state_dim = state_dim

        self.lookup = LookupTable(name='words_embeddings')
        self.bidir = BidirectionalWMT15(
            GatedRecurrent(activation=Tanh(), dim=state_dim))
        self.fwd_fork = Fork(
            [name for name in self.bidir.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='words_fwd_fork')
        self.back_fork = Fork(
            [name for name in self.bidir.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='words_back_fork')

        self.children = [self.lookup, self.bidir, self.fwd_fork, self.back_fork]

    def _push_allocation_config(self):
        self.lookup.length = self.vocab_size
        self.lookup.dim = self.embedding_dim

        self.fwd_fork.input_dim = self.embedding_dim
        self.fwd_fork.output_dims = [self.bidir.children[0].get_dim(name)
                                     for name in self.fwd_fork.output_names]
        self.back_fork.input_dim = self.embedding_dim
        self.back_fork.output_dims = [self.bidir.children[1].get_dim(name)
                                      for name in self.back_fork.output_names]

    @application(inputs=['words', 'words_mask'],
                 outputs=['representation'])
    def apply(self, words, words_mask):
        # Time as first dimension
        words = words.T
        words_mask = words_mask.T

        embeddings = self.lookup.apply(words)
        representation = self.bidir.apply(
            merge(self.fwd_fork.apply(embeddings, as_dict=True),
                  {'mask': words_mask}),
            merge(self.back_fork.apply(embeddings, as_dict=True),
                  {'mask': words_mask})
        )
        return representation


class BidirectionalAudioEncoder(Initializable):

    def __init__(self, feature_size, embedding_dim, state_dim, **kwargs):
        super(BidirectionalAudioEncoder, self).__init__(**kwargs)
        self.feature_size = feature_size
        self.embedding_dim = embedding_dim
        self.state_dim = state_dim

        self.embedding = BidirectionalWMT15(GatedRecurrent(activation=Tanh(), dim=state_dim), name="audio_embeddings")
        self.embedding_fwd_fork = Fork(
            [name for name in self.embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='embedding_fwd_fork')
        self.embedding_back_fork = Fork(
            [name for name in self.embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='embedding_back_fork')

        self.bidir = BidirectionalWMT15(GatedRecurrent(activation=Tanh(), dim=state_dim), name="audio_representation")
        self.fwd_fork = Fork(
            [name for name in self.bidir.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='fwd_fork')
        self.back_fork = Fork(
            [name for name in self.bidir.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='back_fork')

        self.children = [self.bidir, self.embedding,
                         self.fwd_fork, self.back_fork, self.embedding_fwd_fork, self.embedding_back_fork]

    def _push_allocation_config(self):
        self.embedding_fwd_fork.input_dim = self.feature_size
        self.embedding_fwd_fork.output_dims = [self.embedding.children[0].get_dim(name) for name in self.embedding_fwd_fork.output_names]
        self.embedding_back_fork.input_dim = self.feature_size
        self.embedding_back_fork.output_dims = [self.embedding.children[1].get_dim(name) for name in self.embedding_back_fork.output_names]

        self.fwd_fork.input_dim = 2 * self.embedding_dim
        self.fwd_fork.output_dims = [self.bidir.children[0].get_dim(name) for name in self.fwd_fork.output_names]
        self.back_fork.input_dim = 2 * self.embedding_dim
        self.back_fork.output_dims = [self.bidir.children[1].get_dim(name) for name in self.back_fork.output_names]


    @application(inputs=['audio', 'audio_mask', 'words_ends', 'words_ends_mask'],
                 outputs=['representation'])
    def apply(self, audio, audio_mask, words_ends, words_ends_mask):
        batch_size = audio.shape[0]
        audio = audio.dimshuffle(1, 0, 2)
        audio_mask = audio_mask.dimshuffle(1, 0)

        embeddings = self.embedding.apply(
            merge(self.embedding_fwd_fork.apply(audio, as_dict=True),
                  {'mask': audio_mask}),
            merge(self.embedding_back_fork.apply(audio, as_dict=True),
                  {'mask': audio_mask})
        )

        rows = tensor.arange(batch_size).reshape((batch_size, 1))
        embeddings = embeddings.dimshuffle(1, 0, 2)[rows, words_ends].dimshuffle(1, 0, 2)

        words_ends_mask = words_ends_mask.dimshuffle(1, 0)
        representation = self.bidir.apply(
            merge(self.fwd_fork.apply(embeddings, as_dict=True),
                  {'mask': words_ends_mask}),
            merge(self.back_fork.apply(embeddings, as_dict=True),
                  {'mask': words_ends_mask})
        )

        return representation


class BidirectionalPhonesEncoder(Initializable):

    def __init__(self, vocab_size, embedding_dim, state_dim, **kwargs):
        super(BidirectionalPhonesEncoder, self).__init__(**kwargs)
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.state_dim = state_dim

        self.lookup = LookupTable(name='phones_embeddings')
        self.embedding = BidirectionalWMT15(GatedRecurrent(activation=Tanh(), dim=state_dim), name="audio_embeddings")
        self.embedding_fwd_fork = Fork(
            [name for name in self.embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='embedding_fwd_fork')
        self.embedding_back_fork = Fork(
            [name for name in self.embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='embedding_back_fork')

        self.bidir = BidirectionalWMT15(GatedRecurrent(activation=Tanh(), dim=state_dim), name="audio_representation")
        self.fwd_fork = Fork(
            [name for name in self.bidir.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='fwd_fork')
        self.back_fork = Fork(
            [name for name in self.bidir.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='back_fork')

        self.children = [self.lookup, self.bidir, self.embedding,
                         self.fwd_fork, self.back_fork, self.embedding_fwd_fork, self.embedding_back_fork]

    def _push_allocation_config(self):
        self.lookup.length = self.vocab_size
        self.lookup.dim = self.embedding_dim

        self.embedding_fwd_fork.input_dim = self.embedding_dim
        self.embedding_fwd_fork.output_dims = [self.embedding.children[0].get_dim(name) for name in self.embedding_fwd_fork.output_names]
        self.embedding_back_fork.input_dim = self.embedding_dim
        self.embedding_back_fork.output_dims = [self.embedding.children[1].get_dim(name) for name in self.embedding_back_fork.output_names]

        self.fwd_fork.input_dim = 2 * self.embedding_dim
        self.fwd_fork.output_dims = [self.bidir.children[0].get_dim(name) for name in self.fwd_fork.output_names]
        self.back_fork.input_dim = 2 * self.embedding_dim
        self.back_fork.output_dims = [self.bidir.children[1].get_dim(name) for name in self.back_fork.output_names]


    @application(inputs=['phones', 'phones_mask', 'phones_words_ends', 'phones_words_ends_mask'],
                 outputs=['representation'])
    def apply(self, phones, phones_mask, phones_words_ends, phones_words_ends_mask):
        batch_size = phones.shape[0]

        phones = self.lookup.apply(phones)
        phones = phones.dimshuffle(1, 0, 2)
        phones_mask = phones_mask.dimshuffle(1, 0)

        embeddings = self.embedding.apply(
            merge(self.embedding_fwd_fork.apply(phones, as_dict=True),
                  {'mask': phones_mask}),
            merge(self.embedding_back_fork.apply(phones, as_dict=True),
                  {'mask': phones_mask})
        )

        rows = tensor.arange(batch_size).reshape((batch_size, 1))
        embeddings = embeddings.dimshuffle(1, 0, 2)[rows, phones_words_ends].dimshuffle(1, 0, 2)

        phones_words_ends_mask = phones_words_ends_mask.dimshuffle(1, 0)
        representation = self.bidir.apply(
            merge(self.fwd_fork.apply(embeddings, as_dict=True),
                  {'mask': phones_words_ends_mask}),
            merge(self.back_fork.apply(embeddings, as_dict=True),
                  {'mask': phones_words_ends_mask})
        )

        return representation


class BidirectionalPhonemeAudioEncoder(Initializable):

    def __init__(self, feature_size, embedding_dim, state_dim, **kwargs):
        super(BidirectionalPhonemeAudioEncoder, self).__init__(**kwargs)
        self.feature_size = feature_size
        self.embedding_dim = embedding_dim
        self.state_dim = state_dim

        self.audio_embedding = BidirectionalWMT15(GatedRecurrent(activation=Tanh(), dim=state_dim), name="audio_embeddings")
        self.audio_fwd_fork = Fork(
            [name for name in self.audio_embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='audio_fwd_fork')
        self.audio_back_fork = Fork(
            [name for name in self.audio_embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='audio_back_fork')

        self.phoneme_embedding = BidirectionalWMT15(GatedRecurrent(activation=Tanh(), dim=state_dim), name="phoneme_embeddings")
        self.phoneme_fwd_fork = Fork(
            [name for name in self.phoneme_embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='phoneme_fwd_fork')
        self.phoneme_back_fork = Fork(
            [name for name in self.phoneme_embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='phoneme_back_fork')

        self.words_embedding = BidirectionalWMT15(GatedRecurrent(activation=Tanh(), dim=state_dim), name="words_embeddings")
        self.words_fwd_fork = Fork(
            [name for name in self.words_embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='words_fwd_fork')
        self.words_back_fork = Fork(
            [name for name in self.words_embedding.prototype.apply.sequences
             if name != 'mask'], prototype=Linear(), name='words_back_fork')

        self.children = [self.phoneme_embedding, self.audio_embedding, self.words_embedding,
                         self.phoneme_fwd_fork, self.phoneme_back_fork, self.audio_fwd_fork, self.audio_back_fork, self.words_fwd_fork, self.words_back_fork]

    def _push_allocation_config(self):
        self.audio_fwd_fork.input_dim = self.feature_size
        self.audio_fwd_fork.output_dims = [self.audio_embedding.children[0].get_dim(name) for name in self.audio_fwd_fork.output_names]
        self.audio_back_fork.input_dim = self.feature_size
        self.audio_back_fork.output_dims = [self.audio_embedding.children[1].get_dim(name) for name in self.audio_back_fork.output_names]

        self.phoneme_fwd_fork.input_dim = 2 * self.embedding_dim
        self.phoneme_fwd_fork.output_dims = [self.phoneme_embedding.children[0].get_dim(name) for name in self.phoneme_fwd_fork.output_names]
        self.phoneme_back_fork.input_dim = 2 * self.embedding_dim
        self.phoneme_back_fork.output_dims = [self.phoneme_embedding.children[1].get_dim(name) for name in self.phoneme_back_fork.output_names]

        self.words_fwd_fork.input_dim = 2 * self.embedding_dim
        self.words_fwd_fork.output_dims = [self.words_embedding.children[0].get_dim(name) for name in self.words_fwd_fork.output_names]
        self.words_back_fork.input_dim = 2 * self.embedding_dim
        self.words_back_fork.output_dims = [self.words_embedding.children[1].get_dim(name) for name in self.words_back_fork.output_names]

    @application(inputs=['audio', 'audio_mask', 'phones_words_acoustic_ends', 'phones_words_acoustic_ends_mask', 'phoneme_words_ends', 'phoneme_words_ends_mask'],
                 outputs=['representation'])
    def apply(self, audio, audio_mask, phones_words_acoustic_ends, phones_words_acoustic_ends_mask, phoneme_words_ends, phoneme_words_ends_mask):
        batch_size = audio.shape[0]
        audio = audio.dimshuffle(1, 0, 2)
        audio_mask = audio_mask.dimshuffle(1, 0)

        audio_embeddings = self.audio_embedding.apply(
            merge(self.audio_fwd_fork.apply(audio, as_dict=True),
                  {'mask': audio_mask}),
            merge(self.audio_back_fork.apply(audio, as_dict=True),
                  {'mask': audio_mask})
        )

        rows = tensor.arange(batch_size).reshape((batch_size, 1))
        phoneme_embeddings = audio_embeddings.dimshuffle(1, 0, 2)[rows, phones_words_acoustic_ends].dimshuffle(1, 0, 2)

        phones_words_acoustic_ends_mask = phones_words_acoustic_ends_mask.dimshuffle(1, 0)
        words_embeddings = self.phoneme_embedding.apply(
            merge(self.phoneme_fwd_fork.apply(phoneme_embeddings, as_dict=True),
                  {'mask': phones_words_acoustic_ends_mask}),
            merge(self.phoneme_back_fork.apply(phoneme_embeddings, as_dict=True),
                  {'mask': phones_words_acoustic_ends_mask})
        )

        words_embeddings = words_embeddings.dimshuffle(1, 0, 2)[rows, phoneme_words_ends].dimshuffle(1, 0, 2)

        phoneme_words_ends_mask = phoneme_words_ends_mask.dimshuffle(1, 0)
        representation = self.words_embedding.apply(
            merge(self.words_fwd_fork.apply(phoneme_embeddings, as_dict=True),
                  {'mask': phoneme_words_ends_mask}),
            merge(self.words_back_fork.apply(phoneme_embeddings, as_dict=True),
                  {'mask': phoneme_words_ends_mask})
        )

        return representation


class GRUInitialState(GatedRecurrent):
    """Gated Recurrent with special initial state.

    Initial state of Gated Recurrent is set by an MLP that conditions on the
    first hidden state of the bidirectional encoder, applies an affine
    transformation followed by a tanh non-linearity to set initial state.

    """
    def __init__(self, attended_dim, **kwargs):
        super(GRUInitialState, self).__init__(**kwargs)
        self.attended_dim = attended_dim
        self.initial_transformer = MLP(activations=[Tanh()],
                                       dims=[attended_dim, self.dim],
                                       name='state_initializer')
        self.children.append(self.initial_transformer)

    @application
    def initial_states(self, batch_size, *args, **kwargs):
        attended = kwargs['attended']
        initial_state = self.initial_transformer.apply(
            attended[0, :, -self.attended_dim:])
        return initial_state

    def _allocate(self):
        self.parameters.append(shared_floatx_nans((self.dim, self.dim),
                               name='state_to_state'))
        self.parameters.append(shared_floatx_nans((self.dim, 2 * self.dim),
                               name='state_to_gates'))
        for i in range(2):
            if self.parameters[i]:
                add_role(self.parameters[i], WEIGHT)


class Decoder(Initializable):
    """Decoder of RNNsearch model."""

    def __init__(self, vocab_size, embedding_dim, state_dim,
                 representation_dim, theano_seed=None, **kwargs):
        super(Decoder, self).__init__(**kwargs)
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.state_dim = state_dim
        self.representation_dim = representation_dim
        self.theano_seed = theano_seed

        # Initialize gru with special initial state
        self.transition = GRUInitialState(
            attended_dim=state_dim, dim=state_dim,
            activation=Tanh(), name='decoder')

        # Initialize the attention mechanism
        self.attention = SequenceContentAttention(
            state_names=self.transition.apply.states,
            attended_dim=representation_dim,
            match_dim=state_dim, name="attention")

        # Initialize the readout, note that SoftmaxEmitter emits -1 for
        # initial outputs which is used by LookupFeedBackWMT15
        readout = Readout(
            source_names=['states', 'feedback',
                          self.attention.take_glimpses.outputs[0]],
            readout_dim=self.vocab_size,
            emitter=SoftmaxEmitter(initial_output=-1, theano_seed=theano_seed),
            feedback_brick=LookupFeedbackWMT15(vocab_size, embedding_dim),
            post_merge=InitializableFeedforwardSequence(
                [Bias(dim=state_dim, name='maxout_bias').apply,
                 Maxout(num_pieces=2, name='maxout').apply,
                 Linear(input_dim=state_dim / 2, output_dim=embedding_dim,
                        use_bias=False, name='softmax0').apply,
                 Linear(input_dim=embedding_dim, name='softmax1').apply]),
            merged_dim=state_dim)

        # Build sequence generator accordingly
        self.sequence_generator = SequenceGenerator(
            readout=readout,
            transition=self.transition,
            attention=self.attention,
            fork=Fork([name for name in self.transition.apply.sequences
                       if name != 'mask'], prototype=Linear())
        )

        self.children = [self.sequence_generator]

    @application(inputs=['representation', 'source_sentence_mask',
                         'target_sentence_mask', 'target_sentence'],
                 outputs=['cost'])
    def cost(self, representation, source_sentence_mask,
             target_sentence, target_sentence_mask):

        source_sentence_mask = source_sentence_mask.T
        target_sentence = target_sentence.T
        target_sentence_mask = target_sentence_mask.T

        # Get the cost matrix
        cost = self.sequence_generator.cost_matrix(**{
            'mask': target_sentence_mask,
            'outputs': target_sentence,
            'attended': representation,
            'attended_mask': source_sentence_mask}
        )

        return (cost * target_sentence_mask).sum() / \
            target_sentence_mask.shape[1]

    @application
    def generate(self, representation, **kwargs):
        length = representation.shape[0]
        batch_size = representation.shape[1]

        return self.sequence_generator.generate(
            n_steps=2 * length,
            batch_size=batch_size,
            attended=representation,
            attended_mask=tensor.ones((batch_size, length)).T,
            **kwargs)
