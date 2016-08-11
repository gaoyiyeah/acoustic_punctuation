import logging
import numpy as np
from theano import tensor
from toolz import merge

from blocks.filter import VariableFilter
from blocks.graph import ComputationGraph
from blocks.initialization import IsotropicGaussian, Orthogonal, Constant
from blocks.model import Model
from blocks.select import Selector

from model import BidirectionalEncoder, BidirectionalAudioEncoder, Decoder

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

def create_model(config):
    if config["input"] == "words":
        encoder, training_representation, sampling_representation = create_word_encoder(config)
        models = [encoder]
    elif config["input"] == "audio":
        encoder, training_representation, sampling_representation = create_audio_encoder(config)
        models = [encoder]
    elif config["input"] == "both":
        words_encoder, words_training_representation, words_sampling_representation = create_word_encoder(config)
        audio_encoder, audio_training_representation, audio_sampling_representation = create_audio_encoder(config)

        def merge_representations(words, audio):
            return tensor.max(tensor.stack([words, audio], axis=0), axis=0)

        training_representation = merge_representations(words_training_representation, audio_training_representation)
        sampling_representation = merge_representations(words_sampling_representation, audio_sampling_representation)
        models = [words_encoder, audio_encoder]

    decoder, cost, samples, search_model = create_decoder(config, training_representation, sampling_representation)
    print_parameteters(models + [decoder])

    return cost, samples, search_model

def create_multitask_model(config):
    words_encoder, words_training_representation, words_sampling_representation = create_word_encoder(config)
    audio_encoder, audio_training_representation, audio_sampling_representation = create_audio_encoder(config)
    models = [words_encoder, audio_encoder]

    decoder, words_cost, words_samples, words_search_model = create_decoder(config, words_training_representation, words_sampling_representation)
    audio_cost, audio_samples, audio_search_model = use_decoder_on_representations(decoder, audio_training_representation, audio_sampling_representation)

    print_parameteters(models + [decoder])
    words_cost = words_cost + audio_cost

    cost = words_cost + audio_cost
    cost.name = "cost"

    return cost, words_samples, words_search_model



def create_word_encoder(config):
    encoder = BidirectionalEncoder(config['src_vocab_size'], config['enc_embed'], config['enc_nhids'])
    encoder.weights_init = IsotropicGaussian(config['weight_scale'])
    encoder.biases_init = Constant(0)
    encoder.push_initialization_config()
    encoder.bidir.prototype.weights_init = Orthogonal()
    encoder.initialize()

    input_words = tensor.lmatrix('words')
    input_words_mask = tensor.matrix('words_mask')
    training_representation = encoder.apply(input_words, input_words_mask)
    training_representation.name = "words_representation"

    sampling_input_words = tensor.lmatrix('sampling_words')
    sampling_input_words_mask = tensor.ones((sampling_input_words.shape[0], sampling_input_words.shape[1]))
    sampling_representation = encoder.apply(sampling_input_words, sampling_input_words_mask)

    return encoder, training_representation, sampling_representation

def create_audio_encoder(config):
    encoder = BidirectionalAudioEncoder(config['audio_feat_size'], config['enc_embed'], config['enc_nhids'])
    encoder.weights_init = IsotropicGaussian(config['weight_scale'])
    encoder.biases_init = Constant(0)
    encoder.push_initialization_config()
    encoder.bidir.prototype.weights_init = Orthogonal()
    encoder.embedding.prototype.weights_init = Orthogonal()
    encoder.initialize()

    audio = tensor.ftensor3('audio')
    audio_mask = tensor.matrix('audio_mask')
    words_ends = tensor.lmatrix('words_ends')
    words_ends_mask = tensor.matrix('words_ends_mask')
    training_representation = encoder.apply(audio, audio_mask, words_ends, words_ends_mask)
    training_representation.name = "audio_representation"

    sampling_audio = tensor.ftensor3('sampling_audio')
    sampling_audio_mask = tensor.ones((sampling_audio.shape[0], sampling_audio.shape[1]))
    sampling_words_ends = tensor.lmatrix('sampling_words_ends')
    sampling_words_ends_mask = tensor.ones((sampling_words_ends.shape[0], sampling_words_ends.shape[1]))
    sampling_representation = encoder.apply(sampling_audio, sampling_audio_mask, sampling_words_ends, sampling_words_ends_mask)

    return encoder, training_representation, sampling_representation

def create_decoder(config, training_representation, sampling_representation):
    decoder = Decoder(config['trg_vocab_size'], config['dec_embed'], config['dec_nhids'], config['enc_nhids'] * 2)
    decoder.weights_init = IsotropicGaussian(config['weight_scale'])
    decoder.biases_init = Constant(0)
    decoder.push_initialization_config()
    decoder.transition.weights_init = Orthogonal()
    decoder.initialize()

    cost, samples, search_model = use_decoder_on_representations(decoder, training_representation, sampling_representation)

    return decoder, cost, samples, search_model

def use_decoder_on_representations(decoder, training_representation, sampling_representation):
    punctuation_marks = tensor.lmatrix('punctuation_marks')
    punctuation_marks_mask = tensor.matrix('punctuation_marks_mask')
    cost = decoder.cost(training_representation, punctuation_marks_mask, punctuation_marks, punctuation_marks_mask)

    generated = decoder.generate(sampling_representation)
    search_model = Model(generated)
    _, samples = VariableFilter(bricks=[decoder.sequence_generator], name="outputs")(ComputationGraph(generated[1]))

    return cost, samples, search_model


def print_parameteters(models):
    param_dict = merge(*[Selector(model).get_parameters() for model in models])
    number_of_parameters = 0

    logger.info("Parameter names: ")
    for name, value in param_dict.items():
        number_of_parameters += np.product(value.get_value().shape)
        logger.info('    {:15}: {}'.format(value.get_value().shape, name))
    logger.info("Total number of parameters: {}".format(number_of_parameters))