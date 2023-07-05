"""
    This file provides a LangChain based approach to using a LLM to determine the weights of edges in a graph, 
        where the graph nodes represent government budget items and socioeconomic/health indicators.

    NOTE: A bug exists within the transformers library that prevents used of 8bit models with automatically inferred device_map parameter to from_pretrained..
        User must go to change the default value of _no_split_modules in transformers.modelling_utils.PretrainedModel to an empty list

    NOTE: currently an issue with kshot prompt generation, currently the prompt is created in the PromptGenerator and System message  in the Predictor. However, prompt message should
            system message should be dependent on the prompt message. This is currently not the case. e.g. if there are k shot examples system message should change to include " there will be some example questions"
        
    
    NOTE: This file expects a json/csv/file that has at least the following columns:
            
"""

import os,sys
sys.path.append(os.getcwd())
from argparse import ArgumentParser

import pandas as pd

from functools import reduce
import json as json

# Testing models to see how well they aligned to expert's annotations of the SPOT dataset with yes_no prompt style w/ rule based parsing and binary weight edge value 

from prompt_engineering.langchain.utils import  HUGGINGFACE_MODELS, OPENAI_MODELS, PredictionGenerator, ALL_MODELS,  MAP_LOAD_IN_NBIT

from prompt_engineering.langchain.utils import load_annotated_examples, load_llm



import yaml
import openai
from prompt_engineering.utils_prompteng import PromptBuilder

from django.core.files.uploadedfile import UploadedFile
import torch
import random

from prompt_engineering.my_logger import setup_logging_predict

def main(
    llm_name:str,
    exp_name:str,
    finetuned:bool,

    predict_b2i:bool,
    predict_i2i:bool,

    prompt_style:str,
    parse_style:str,

    ensemble_size:int,
    effect_type:str, 
    edge_value:str,

    input_file:str|UploadedFile,

    k_shot_b2i:int=2,
    k_shot_i2i:int=0,

    k_shot_example_dset_name_b2i:str = 'spot',
    k_shot_example_dset_name_i2i:str|None = None,

    unbias_categorisations:bool = False,

    local_or_remote:str='remote',
    api_key:str|None = None,

    batch_size:int=1,

    deepspeed_compat=False,

    save_output:bool = False,

    debugging:bool= False,
    data_load_seed:int = 10):
    
    assert (predict_b2i is True and predict_i2i is False) or (predict_b2i is False and predict_i2i is True), "Only one of predict_b2i or predict_i2i can be true"
    
    if prompt_style == 'yes_no':
        assert parse_style == 'rules'
    elif prompt_style == 'open':
        assert parse_style =='category_rules
    elif prompt_style == 'categorise':
        assert parse_style == 'perplexity'
    elif prompt_style == 'cot_categorise':
        assert parse_style == 'categories_perplexity'
    else:
        raise ValueError(f"Invalid prompt_style: {prompt_style}")

    # Setup Logging
    logging = setup_logging_predict(llm_name)

    logging.info("Starting Prediction Script with model: {}".format(llm_name))

    # Load LLM
    logging.info(f"\tLoading {llm_name}")
    try:
        llm =  load_llm(llm_name, finetuned, local_or_remote, api_key)
    except Exception as e:
        logging.error(f"Error loading LLM: {e}")
        raise e

    # Load Annotated Examples to use in K-Shot context for Prompt
    logging.info("\tLoading Annotated Examples")
    annotated_examples_b2i =  None if predict_b2i is False else  load_annotated_examples(k_shot_example_dset_name_b2i, relationship_type='budgetitem_to_indicator' )
    annotated_examples_i2i = None if predict_i2i is False else load_annotated_examples(k_shot_example_dset_name_i2i, relationship_type='indicator_to_indicator')
    logging.info("\Annotated Examples Loaded")

    # Create Prompt Builders
    logging.info("\tCreating Prompt Builders")
    prompt_builder_b2i = None if predict_b2i is False else PromptBuilder(prompt_style, k_shot_b2i,
                                        ensemble_size, annotated_examples_b2i, 
                                        effect_type,
                                        relationship='budgetitem_to_indicator',
                                        seed=data_load_seed,
                                        )
    
    prompt_builder_i2i: PromptBuilder | None = None if predict_i2i is False else PromptBuilder(prompt_style, k_shot_i2i,
                                                                           ensemble_size, annotated_examples_i2i, 
                                                                           effect_type,
                                                                           relationship='indicator_to_indicator',
                                                                           seed=data_load_seed)
    logging.info("\tPrompt Builders Created")

    # Create Prediction Generators
    logging.info("\tCreating Prediction Generators")
    prediction_generator_b2i = None if predict_b2i is False else PredictionGenerator(llm,
                                                        llm_name,
                                                        prompt_style,
                                                        ensemble_size,
                                                        edge_value,
                                                        parse_style,
                                                        relationship='budgetitem_to_indicator',
                                                        local_or_remote=local_or_remote,
                                                        deepspeed_compat=deepspeed_compat,
                                                        effect_type=effect_type
                                                        )
    prediction_generator_i2i = None if predict_i2i is False else PredictionGenerator(llm, 
                                                        llm_name,
                                                        prompt_style,
                                                        ensemble_size,
                                                        edge_value,
                                                        parse_style,
                                                        relationship='indicator_to_indicator',
                                                        local_or_remote=local_or_remote,
                                                        deepspeed_compat=deepspeed_compat,
                                                        effect_type=effect_type
                                                        )
    logging.info("\tPrediction Generators Created")
        
    # prepare data
    logging.info("\tPreparing Data")
    li_record_b2i  = None if predict_b2i is False else prepare_data_b2i(input_file,
                                                            debugging=debugging,
                                                            data_load_seed=data_load_seed,
                                                            logging=logging )
    
    li_record_i2i = None if predict_i2i is False else prepare_data_i2i(input_file,
                                                            debugging=debugging,
                                                            data_load_seed=data_load_seed,
                                                            logging=logging )
    logging.info("\tData Prepared")
    

    # run predictions
    logging.info("\tRunning Predictions")
    (li_prompt_ensemble_b2i, li_pred_ensemble_b2i,
        li_pred_ensemble_parsed_b2i, li_pred_agg_b2i, 
        li_prompt_ensemble_fmtd_b2i) = (None, None, None, None, None) if (predict_b2i is False) else predict_batches( prompt_builder_b2i, prediction_generator_b2i, li_record_b2i, batch_size, unbias_categorisations, logging) # type: ignore #ignore
        
    (li_prompt_ensemble_i2i, li_pred_ensemble_i2i,
         li_pred_ensemble_parsed_i2i, li_pred_agg_i2i,
         li_prompt_ensemble_fmtd_i2i) = (None, None, None, None, None) if (predict_i2i is False) else predict_batches( prompt_builder_i2i, prediction_generator_i2i, li_record_i2i, batch_size, unbias_categorisations, logging) #type: ignore 
    logging.info("\tPredictions Complete")

    # saving to file
    if save_output:
        logging.info("\tSaving Output")
        experiment_config = {
                            "llm_name": llm_name,
                            "exp_name": exp_name,
                            'codebase': 'langchain',
                            "finetuned": finetuned,
                            "prompt_style": prompt_style,
                            "parse_style": parse_style,
                            "ensemble_size": ensemble_size,
                            "effect_type": effect_type,
                            "edge_value": edge_value,
                            "k_shot_b2i": k_shot_b2i,
                            "k_shot_i2i": k_shot_i2i,
                            "k_shot_example_dset_name_b2i": k_shot_example_dset_name_b2i,
                            "k_shot_example_dset_name_i2i": k_shot_example_dset_name_i2i,
                            "local_or_remote": local_or_remote,
                            "deepspeed_compat": deepspeed_compat,
                        }
        
        # Save experiment config
        dir_experiments = os.path.join('prompt_engineering','output','spot','exp_runs' )
        os.makedirs(dir_experiments, exist_ok=True )

        existing_numbers = [int(x.split('_')[-1]) for x in os.listdir(dir_experiments) if x.startswith(f'exp_{exp_name}') ]
        lowest_available_number = min(set(range(1000)) - set(existing_numbers))
        experiment_number = lowest_available_number

        save_dir = os.path.join(dir_experiments, f"exp_{exp_name}_{experiment_number:03d}" )
        os.makedirs(save_dir, exist_ok=True )
        
        with open(os.path.join(save_dir, 'config.json'), 'w') as f:
            yaml.safe_dump(experiment_config, f)

        #unbatching data
        if predict_b2i: 
            save_experiment(li_record_b2i, li_prompt_ensemble_b2i, li_prompt_ensemble_fmtd_b2i, li_pred_ensemble_b2i, li_pred_ensemble_parsed_b2i, li_pred_agg_b2i, relationship='budgetitem_to_indicator', save_dir=save_dir) #type: ignore
        
        if predict_i2i: 
            save_experiment( li_record_i2i, li_prompt_ensemble_i2i, li_prompt_ensemble_fmtd_i2i, li_pred_ensemble_i2i, li_pred_ensemble_parsed_i2i, li_pred_agg_i2i, relationship='indicator_to_indicator', save_dir=save_dir) #type: ignore #ignore
        
        logging.info("\tOutput Saved")

    return {
        'li_record_b2i': li_record_b2i,
        'li_prompt_ensemble_b2i': li_prompt_ensemble_b2i,
        'li_pred_ensemble_b2i': li_pred_ensemble_b2i,
        'li_pred_ensemble_parsed_b2i': li_pred_ensemble_parsed_b2i,
        'li_pred_agg_b2i': li_pred_agg_b2i,

        'li_record_i2i': li_record_i2i,
        'li_prompt_ensemble_i2i': li_prompt_ensemble_i2i,
        'li_pred_ensemble_i2i': li_pred_ensemble_i2i,
        'li_pred_ensemble_parsed_i2i': li_pred_ensemble_parsed_i2i,
        'li_pred_agg_i2i': li_pred_agg_i2i,
    }

def prepare_data_b2i(input_file:str|UploadedFile, debugging=False, data_load_seed=10, 
                  logging=None ) -> tuple[list[dict[str,str]]|None, list[dict[str,str]]|None]:
    """
        Loads the data from the input_file and returns a list of lists of dicts

        Data can be passed in as a json or csv file name, or as a Django UploadedFile Object

        If labels are supplied it is assumed that budget_items, indicators and labels are all index aligned
    """
    
    # Check json is valid
    expected_keys = ['budget_item','indicator']
    
    # Load data
    # Data cn be passed in as a json or csv file name, or as a Django UploadedFile Object
    if isinstance(input_file, str) and input_file[-4:]=='.json':
        json_data = json.load( open(input_file, 'r') )
        assert all([key in json_data.keys() for key in expected_keys]), f"input_json must have the following keys: {expected_keys}"
        
        li_budget_items = json_data['budget_item']
        li_indicator  = json_data['indicator']

        # set_budget_items = sorted(set(li_budget_items))
        # set_indicator = sorted(set(li_indicator))

        li_labels = json_data.get('label', [None]*len(li_budget_items) )
    
    elif isinstance(input_file, str) and input_file[-4:] == '.csv':
        df = pd.read_csv(input_file)
        assert all([key in df.columns for key in expected_keys]), f"input_csv must have the following columns: {expected_keys}"

        li_budget_items = df['budget_item'].tolist()
        li_indicator = df['indicator'].tolist()
        li_labels = df['label'].tolist() if 'label' in df.columns else [None]*len(li_budget_items)

        # set_budget_items = sorted(set(li_budget_items))
        # set_indicator = sorted(set(li_indicator))

    elif isinstance(input_file, UploadedFile):
        json_data = input_file
        raise NotImplementedError("UploadedFile not implemented yet")
        li_budget_items = json_data['budget_item']
        li_indicator = json_data['indicator']
        li_labels = [None]*len(li_budget_items)
    
    else:
        raise NotImplementedError(f"input_file must be a json or csv file name, or a Django UploadedFile Object, not {input_file}")
    
    # Creating all possible combinations of budget_items and indicators
    li_record_b2i = [ {'budget_item':budget_item, 'indicator':indicator, 'label':label  } for budget_item, indicator, label in zip( li_budget_items, li_indicator, li_labels) ] 
    
    if debugging:
        random.seed(data_load_seed)
        li_record_b2i = random.sample(li_record_b2i, 3)
    
    return li_record_b2i # type: ignore

def prepare_data_i2i(input_file:str|UploadedFile, debugging=False, data_load_seed=10, 
                  logging=None ) -> tuple[list[dict[str,str]]|None, list[dict[str,str]]|None]:
    """
        Loads the data from the input_file and returns a list of lists of dicts

        Data can be passed in as a json or csv file name, or as a Django UploadedFile Object

        If labels are supplied it is assumed that budget_items, indicators and labels are all index aligned
    """
    
    # Check json is valid
    random.seed(data_load_seed)
    expected_keys = ['indicator1','indicator2']
    
    # Load data
    # Data cn be passed in as a json or csv file name, or as a Django UploadedFile Object
    if isinstance(input_file, str) and input_file[-4:]=='.json':
        json_data = json.load( open(input_file, 'r') )
        assert all([key in json_data.keys() for key in expected_keys]), f"input_json must have the following keys: {expected_keys}"
        
        li_indicator1 = json_data['indicator1']
        li_indicator2  = json_data['indicator2']
        li_labels = json_data.get('label', [None]*len(li_indicator1) )
    
    elif isinstance(input_file, str) and input_file[-4:] == '.csv':
        df = pd.read_csv(input_file)
        assert all([key in df.columns for key in expected_keys]), f"input_csv must have the following columns: {expected_keys}"

        li_indicator1 = df['indicator1'].tolist()
        li_indicator2 = df['indicator2'].tolist()
        li_labels = df['label'].tolist() if 'label' in df.columns else [None]*len(li_indicator1)

    elif isinstance(input_file, UploadedFile):
        json_data = input_file
        raise NotImplementedError("UploadedFile not implemented yet")
        li_indicator1 = json_data['indicator1']
        li_indicator2 = json_data['indicator2']
        li_labels = [None]*len(li_indicator1)
    
    else:
        raise NotImplementedError(f"input_file must be a json or csv file name, or a Django UploadedFile Object, not {input_file}")
    

    # Creating all possible combinations of budget_items and indicators
    li_record_i2i = [ {'indicator1':indicator1, 'indicator2':indicator2, 'label':label  } for indicator1, indicator2, label in zip( li_indicator1, li_indicator2, li_labels) ] 
    
    if debugging:
        li_record_i2i = random.sample(li_record_i2i, 3)
    
    return li_record_i2i

def predict_batches(prompt_builder:PromptBuilder, 
                        prediction_generator:PredictionGenerator, 
                        li_record:list[dict[str,str]],
                        batch_size=2,
                        unbias_categorisations:bool=False,
                        logger=None ) -> tuple[list[list[str]], list[list[str]], list[list[str]], list[str], list[str]]:

    # Creating Predictions for each row in the test set
    li_prompt_ensemble = []
    li_prompt_ensemble_fmtd = []
    li_pred_ensemble = []
    li_pred_ensemble_parsed = []
    li_pred_agg = []

    li_li_record = [ li_record[i:i+batch_size] for i in range(0, len(li_record), batch_size) ]

    for idx, batch in enumerate(li_li_record):
        if logger is not None:
            logger.info(f"Predicting batch {idx+1} of {len(li_li_record)}")

        # Create prompts
        batch_prompt_ensembles = prompt_builder(batch)
        
        # Generate predictions
        batch_li_prompts_fmtd, batch_pred_ensembles, batch_pred_ensembles_parsed = prediction_generator.predict(batch_prompt_ensembles)

        # Generate any predictions with category order reversed
        if unbias_categorisations:
            batch_prompt_ensembles_reversed = prompt_builder(batch, reverse_order=True)
            batch_li_prompts_fmtd_reversed, batch_pred_ensembles_reversed, batch_pred_ensembles_parsed_reversed = prediction_generator.predict(batch_prompt_ensembles_reversed, reverse_categories=True)

            # Merge the two sets of outputs, datum for datum merge
            batch_li_prompts_fmtd = [ li_prompts_fmtd + li_prompts_fmtd_reversed for li_prompts_fmtd, li_prompts_fmtd_reversed in zip(batch_li_prompts_fmtd, batch_li_prompts_fmtd_reversed) ]
            batch_pred_ensembles = [ pred_ensembles + pred_ensembles_reversed for pred_ensembles, pred_ensembles_reversed in zip(batch_pred_ensembles, batch_pred_ensembles_reversed) ]
            batch_pred_ensembles_parsed = [ pred_ensembles_parsed + pred_ensembles_parsed_reversed for pred_ensembles_parsed, pred_ensembles_parsed_reversed in zip(batch_pred_ensembles_parsed, batch_pred_ensembles_parsed_reversed) ]
            
            # Let the aggregation step handle the rest

        # Aggregate ensembles into predictions
        batch_pred_agg = prediction_generator.aggregate_predictions(batch_pred_ensembles_parsed)



        # Extract predictions from the generated text
        li_prompt_ensemble.extend(batch_prompt_ensembles)  # type: ignore
        li_prompt_ensemble_fmtd.extend(batch_li_prompts_fmtd) # type: ignore
        li_pred_ensemble.extend( batch_pred_ensembles ) # type: ignore
        li_pred_ensemble_parsed.extend( batch_pred_ensembles_parsed ) # type: ignore
        li_pred_agg.extend(batch_pred_agg) # type: ignore
    
    return li_prompt_ensemble, li_pred_ensemble, li_pred_ensemble_parsed, li_pred_agg, li_prompt_ensemble_fmtd

def save_experiment( 
    li_record:list[dict[str,str]],
    li_prompt_ensemble:list[list[str]],
    li_prompt_ensemble_fmtd:list[list[str]],
    li_pred_ensemble:list[list[str]],
    li_pred_ensemble_parsed:list[list[str]],
    li_pred_agg,
    relationship:str='budgetitem_to_indicator',
    save_dir:str='experiments' ):
    
    # Save predictions as csv files with the following columns ['prediction_aggregated', 'prompts', 'predictions', 'predictions_parsed']
    encode = lambda _list: [ json.dumps(val) for val in _list]
    
    li_prompt_ensemble_fmtd = li_prompt_ensemble_fmtd if len(li_prompt_ensemble_fmtd) > 0 else [None]*len(li_prompt_ensemble)
    if relationship == 'budgetitem_to_indicator':
        df = pd.DataFrame({ 
                        'budget_item': [ d['budget_item'] for d in li_record],
                       'indicator': [ d['indicator'] for d in li_record],
                       'pred_aggregated':li_pred_agg, 
                       'prompts':encode(li_prompt_ensemble), 
                       'predictions':encode(li_pred_ensemble), 
                       'predictions_parsed':encode(li_pred_ensemble_parsed),
                       'prompts_fmtd':encode(li_prompt_ensemble_fmtd) })
        if 'label' in li_record[0].keys():
            df['label'] = [ d['label'] for d in li_record]
            # reorder df columns to be 'budget_item', 'indicator', 'label', 'prediction_aggregated', 'prompts', 'predictions', 'predictions_parsed'
            df = df[['budget_item', 'indicator', 'label', 'pred_aggregated', 'prompts', 'predictions', 'predictions_parsed', 'prompts_fmtd']]


    elif relationship == 'indicator_to_indicator':
        df = pd.DataFrame({ 'indicator_1': [ d['indicator_1'] for d in li_record],
                           'indicator_2': [ d['indicator_2'] for d in li_record],
                        #    'label': [ d['label'] for d in li_record],
                        'prediction_aggregated':li_pred_agg, 'prompts':encode(li_prompt_ensemble), 
                       'predictions':encode(li_pred_ensemble), 'predictions_parsed':encode(li_pred_ensemble_parsed), 'prompts_fmtd':encode(li_prompt_ensemble_fmtd)})
        if 'label' in li_record[0].keys():
            df['label'] = [ d['label'] for d in li_record]
            # reorder df columns to be 'budget_item', 'indicator', 'label', 'prediction_aggregated', 'prompts', 'predictions', 'predictions_parsed'
            df = df[['indicator1', 'indicator2', 'label', 'prediction_aggregated', 'prompts', 'predictions', 'predictions_parsed']]
                
    else:
        raise ValueError("relationship must be one of ['budgetitem_to_indicator', 'indicator_to_indicator']")

    # Save to csv
    os.makedirs(save_dir, exist_ok=True)
    name = None
    if relationship == 'budgetitem_to_indicator':
        name = 'b2i'
    elif relationship == 'indicator_to_indicator':
        name = 'i2i'
    df.to_csv(os.path.join(save_dir, f'predictions_{name}.csv'), index=False)

    return None

def parse_args():
    
    parser = ArgumentParser(add_help=True, allow_abbrev=False)
    parser.add_argument('--llm_name', type=str, default='mosaicml/mpt-7b-chat', choices=ALL_MODELS )
    parser.add_argument('--exp_name', type=str, default='mpt7b', required=True )

    
    parser.add_argument('--predict_b2i', action='store_true', default=True, help='Indicates whether to predict budgetitem to indicator' )
    parser.add_argument('--predict_i2i', action='store_true', default=False, help='Indicates whether to predict indicator to indicator' )

    parser.add_argument('--finetuned', action='store_true', default=False, help='Indicates whether a finetuned version of nn_name should be used' )
    
    parser.add_argument('--prompt_style',type=str, choices=['yes_no','open', 'categorise', 'cot_categorise' ], default='open', help='Style of prompt' )
    parser.add_argument('--parse_style', type=str, choices=['rules', 'category_rules', 'category_perplexity'], default='categories_perplexity', help='How to convert the output of the model to a Yes/No Output' )

    parser.add_argument('--ensemble_size', type=int, default=1 )
    parser.add_argument('--effect_type', type=str, default='arbitrary', choices=['arbitrary', 'directly', 'indirectly'], help='Type of effect to ask language model to evaluate' )
    parser.add_argument('--edge_value', type=str, default='binary_weight', choices=['binary_weight', 'distribution'], help='' )

    parser.add_argument('--input_file', type=str, default='"./data/spot/spot_indicator_mapping_table_test.csv"', help='Path to the file containing the input data' )

    parser.add_argument('--k_shot_b2i', type=int, default=0, help='Number of examples to use for each prompt for the budget_item to indicator predictions' )
    parser.add_argument('--k_shot_i2i', type=int, default=0, help='Number of examples to use for each prompt for the indicator to indicator predictions' )

    parser.add_argument('--k_shot_example_dset_name_b2i', type=lambda inp: None if inp.lower()=="none" else str(inp), default='spot', choices=['spot','england', None], help='The dataset to use for the k_shot examples for the budget_item to indicator predictions' )
    parser.add_argument('--k_shot_example_dset_name_i2i', type= lambda inp: None if inp.lower()=="none" else str(inp), default=None, choices=['spot','england',None], help='The dataset to use for the k_shot examples for the indicator to indicator predictions' )

    parser.add_argument('--unbias_categorisations', action='store_true', default=False, help='Indicates whether to take measures to reduce bias towards category N when using categorisation type methods to answer questions' )

    parser.add_argument('--local_or_remote', type=str, default='local', choices=['local','remote'], help='Whether to use llms on a remote server or locally' )
    parser.add_argument('--api_key', type=str, default=None, help='The api key for the remote server e.g. HuggingfaceHub or OpenAIapi' )
    
    parser.add_argument('--batch_size', type=int, default=1 )

    parser.add_argument('--data_load_seed', type=int, default=10, help='The seed to use when loading the data' )

    parser.add_argument('--save_output', action='store_true', default=False, help='Indicates whether the output should be saved' )

    parser.add_argument('--debugging', action='store_true', default=False, help='Indicates whether to run in debugging mode' )

    
    args = parser.parse_known_args()[0]

    return args


if __name__ == "__main__":
    args = parse_args()
    main(**vars(args))