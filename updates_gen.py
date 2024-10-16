import os
import json
import random
import re
import argparse
import logging
from time import sleep
from typing import List, Dict
from datasets import load_dataset
from openai import OpenAI, OpenAIError
from tqdm import tqdm
import spacy
from dotenv import load_dotenv
import hashlib

# Initialize logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("dialogue_generation.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load spaCy's English model for NER
try:
    nlp = spacy.load("en_core_web_sm")
except OSError:
    logger.info("spaCy model not found. Downloading 'en_core_web_sm'...")
    import subprocess
    subprocess.run(["python", "-m", "spacy", "download", "en_core_web_sm"])
    nlp = spacy.load("en_core_web_sm")

load_dotenv('.env.local')

# Initialize OpenAI client
openai_api_key = os.getenv('OPENAI_KEY')
if not openai_api_key:
    logger.error("OPENAI_API_KEY environment variable not set.")
    exit(1)
client = OpenAI(api_key=openai_api_key)

def anonymize_text(text: str) -> str:
    """
    Anonymize specific entities in the text such as locations, times, and numbers.
    """
    doc = nlp(text)
    anonymized_text = text
    # Define entity replacements
    replacements = {
        "GPE": "LOCATION",
        "LOC": "LOCATION",
        "TIME": "TIME",
        "DATE": "DATE",
        "CARDINAL": "NUMBER",
        "ORDINAL": "NUMBER",
        "MONEY": "AMOUNT",
        "PERSON": "PERSON",
        "ORG": "ORGANIZATION"
    }

    # Sort entities by start index in reverse to avoid offset issues during replacement
    entities = sorted(doc.ents, key=lambda ent: ent.start_char, reverse=True)

    for ent in entities:
        if ent.label_ in replacements:
            placeholder = f"<{replacements[ent.label_].upper()}>"
            anonymized_text = anonymized_text[:ent.start_char] + placeholder + anonymized_text[ent.end_char:]

    return anonymized_text

def extract_and_anonymize_dialogue(dialogue_json: Dict) -> List[Dict]:
    """
    Extracts turns from the dialogue JSON and anonymizes the utterances.
    Returns a list of turns with anonymized utterances.
    """
    turns = []
    speakers = dialogue_json.get("speaker", [])
    utterances = dialogue_json.get("utterance", [])
    turn_ids = dialogue_json.get("turn_id", [])

    for turn_id, speaker, utterance in zip(turn_ids, speakers, utterances):
        if speaker == 0:
            speaker_label = "USER"
        elif speaker == 1:
            speaker_label = "ASSISTANT"
        else:
            speaker_label = "UNKNOWN"

        anonymized_utterance = anonymize_text(utterance)

        turns.append({
            "turn_id": turn_id,
            "speaker": speaker_label,
            "utterance": anonymized_utterance
        })

    return turns

def generate_base_conversation(turns: List[Dict]) -> str:
    """
    Formats the list of turns into a base conversation string.
    """
    conversation = ""
    for turn in turns:
        conversation += f"{turn['speaker']}: {turn['utterance']}\n"
    return conversation.strip()

def load_existing_hashes(output_file: str, hash_file: str = 'dialogue_hashes.json') -> set:
    """
    Loads existing dialogue hashes from a hash file or the output JSON file.
    """
    if os.path.exists(hash_file):
        try:
            with open(hash_file, 'r', encoding='utf-8') as f:
                hashes = set(json.load(f))
            logger.info(f"Loaded {len(hashes)} existing dialogue hashes from '{hash_file}'.")
            return hashes
        except Exception as e:
            logger.warning(f"Could not load existing hashes: {e}")
    elif os.path.exists(output_file):
        # Fallback to existing output file
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                existing_dialogues = json.load(f)
                hashes = set()
                for dialogue in existing_dialogues:
                    dialogue_text = dialogue.get('base_conversation', '')
                    dialogue_hash = hashlib.sha256(dialogue_text.encode('utf-8')).hexdigest()
                    hashes.add(dialogue_hash)
            logger.info(f"Loaded {len(hashes)} existing dialogue hashes from '{output_file}'.")
            # Save to hash file for future runs
            with open(hash_file, 'w', encoding='utf-8') as hf:
                json.dump(list(hashes), hf, indent=4)
            return hashes
        except Exception as e:
            logger.warning(f"Could not load existing dialogues: {e}")
    return set()

def generate_dialogue(service, prompt, min_turns, max_turns, max_retries=3):
    """
    Generates a dialogue using OpenAI's chat completions API with uniqueness checks.
    """
    try:
        system_prompt = (
            f"You are an expert dialogue generator for the '{service}' service. "
            f"Create a high-quality, coherent, and relevant dialogue between a user and an assistant. "
            f"The dialogue should have between {min_turns} and {max_turns} turns (a turn is one user message and one assistant response). "
            f"The dialogue should not be the same as any existing dialogues and should be better and more engaging.\n\n"
            f"Please format the dialogue as follows, with each user message starting with 'User:' and each assistant response starting with 'Assistant:'.\n"
            f"Example:\n"
            f"User: Hello!\n"
            f"Assistant: Hi there! How can I assist you today?\n"
        )

        for attempt in range(1, max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model='gpt-4o-mini',  # Ensure the correct model name is used
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=1500,
                    temperature=0.9,  # Adjusted temperature for balance between creativity and coherence
                    top_p=0.95,
                    frequency_penalty=0.5,
                    presence_penalty=0.5,
                    n=3,  # Generate 3 completions to select from
                )
                generated_dialogues = [choice.message.content.strip() for choice in response.choices]

                for gen_dialogue in generated_dialogues:
                    # Check if the dialogue contains expected speaker labels
                    if re.search(r'^(User:|Assistant:)', gen_dialogue, re.MULTILINE):
                        return gen_dialogue  # Return the first valid formatted dialogue

                logger.warning(f"Attempt {attempt} - No valid dialogue found in generated completions.")
                if attempt < max_retries:
                    sleep(2 ** attempt)  # Exponential backoff
                else:
                    logger.error(f"Failed to generate properly formatted dialogue after {max_retries} attempts.")
                    return None
            except OpenAIError as e:
                logger.warning(f"Attempt {attempt} - OpenAI API error: {e}")
                if attempt < max_retries:
                    sleep(2 ** attempt)  # Exponential backoff
                else:
                    logger.error(f"Failed after {max_retries} attempts.")
                    return None
    except Exception as e:
        logger.error(f"Unexpected error in generate_dialogue: {e}")
        return None

def process_generated_dialogue(generated_dialogue: str) -> List[Dict]:
    """
    Processes the generated dialogue text into a list of turns.
    """
    generated_turns = []
    for line in generated_dialogue.split('\n'):
        line = line.strip()
        if line:
            if line.lower().startswith('user:'):
                speaker = 'USER'
                utterance = line.split(':', 1)[1].strip()
            elif line.lower().startswith(('assistant:', 'system:', 'agent:')):
                speaker = 'ASSISTANT'
                utterance = line.split(':', 1)[1].strip()
            else:
                continue
            generated_turns.append({
                'speaker': speaker,
                'utterance': utterance
            })
    return generated_turns

def parse_arguments():
    parser = argparse.ArgumentParser(description="Generate dialogues using OpenAI API.")
    parser.add_argument('--num_generations', type=int, required=True, help="Number of dialogues to generate.")
    parser.add_argument('--min_turns', type=int, default=3, help="Minimum number of dialogue turns.")
    parser.add_argument('--max_turns', type=int, default=10, help="Maximum number of dialogue turns.")
    parser.add_argument('--output_file', type=str, default='generated_dialogues.json', help="Output JSON file path.")
    return parser.parse_args()

def main():
    args = parse_arguments()

    num_generations = args.num_generations
    min_turns = args.min_turns
    max_turns = args.max_turns
    output_file = args.output_file

    logger.info("Starting dialogue generation...")
    logger.info(f"Parameters: num_generations={num_generations}, min_turns={min_turns}, max_turns={max_turns}, output_file='{output_file}'")

    # Load dataset from Hugging Face
    try:
        dataset = load_dataset('Ayushnangia/transport_multiwoz_v22')
        data_split = dataset['train']
        logger.info("Dataset loaded successfully.")
    except Exception as e:
        logger.error(f"Failed to load dataset: {e}")
        return

    # Load existing dialogues and their hashes
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                existing_dialogues = json.load(f)
                existing_ids = {dialogue['dialogue_id'] for dialogue in existing_dialogues}
            logger.info(f"Loaded {len(existing_dialogues)} existing dialogues from '{output_file}'.")
        except Exception as e:
            logger.warning(f"Could not load existing dialogues: {e}")
            existing_dialogues = []
            existing_ids = set()
    else:
        existing_dialogues = []
        existing_ids = set()

    existing_hashes = load_existing_hashes(output_file, 'dialogue_hashes.json')

    # Prepare list to collect new dialogues
    new_dialogues = []

    # Randomly select examples to generate dialogues for
    if num_generations > len(data_split):
        logger.error("Number of generations requested exceeds the dataset size.")
        return

    selected_indices = random.sample(range(len(data_split)), num_generations)

    for index in tqdm(selected_indices, desc="Generating dialogues"):
        example = data_split[index]
        services = example.get('services', [])
        dialogue_id = example.get('dialogue_id', f"dialogue_{index}")

        # Extract and anonymize existing dialogue
        processed_dialogue = extract_and_anonymize_dialogue(example)
        base_conversation = generate_base_conversation(processed_dialogue)

        # Create hash of the base conversation to check for duplicates
        dialogue_hash = hashlib.sha256(base_conversation.encode('utf-8')).hexdigest()
        if dialogue_hash in existing_hashes:
            logger.info(f"Duplicate dialogue detected for dialogue_id '{dialogue_id}'. Skipping.")
            continue

        prompt = (
            f"Using the following base conversation as a reference, create a new dialogue for the service(s): {', '.join(services)}. "
            f"The dialogue should be completely new and more relevant than any existing dialogue. Do not copy any part of existing dialogues. "
            f"The dialogue should be between a user and an assistant.\n\n"
            f"Base Conversation:\n{base_conversation}"
        )

        generated_dialogue = generate_dialogue(services[0] if services else "general", prompt, min_turns, max_turns)

        if generated_dialogue:
            generated_turns = process_generated_dialogue(generated_dialogue)
            generated_conversation = generate_base_conversation(generated_turns)
            generated_hash = hashlib.sha256(generated_conversation.encode('utf-8')).hexdigest()

            if generated_hash in existing_hashes:
                logger.warning(f"Generated dialogue is a duplicate for dialogue_id '{dialogue_id}'. Skipping.")
                continue

            new_dialogue_id = f"{dialogue_id}_generated_{index}"
            if new_dialogue_id in existing_ids:
                logger.warning(f"Duplicate dialogue_id '{new_dialogue_id}' found. Skipping.")
                continue

            new_dialogues.append({
                'services': services,
                'dialogue_id': new_dialogue_id,
                'turns': generated_turns,
                'base_conversation': generated_conversation
            })
            existing_ids.add(new_dialogue_id)
            existing_hashes.add(generated_hash)

    logger.info("Dialogue generation complete.")

    # Combine existing and new dialogues
    all_dialogues = existing_dialogues + new_dialogues

    # Save the new dialogues to a JSON file
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_dialogues, f, indent=4, ensure_ascii=False)
        logger.info(f"Generated dialogues saved to '{output_file}'. Total dialogues: {len(all_dialogues)}.")
    except Exception as e:
        logger.error(f"Failed to save dialogues to '{output_file}': {e}")

    # Update the dialogue hashes
    try:
        with open('dialogue_hashes.json', 'w', encoding='utf-8') as hf:
            json.dump(list(existing_hashes), hf, indent=4)
        logger.info(f"Updated 'dialogue_hashes.json' with {len(existing_hashes)} hashes.")
    except Exception as e:
        logger.error(f"Failed to update 'dialogue_hashes.json': {e}")

if __name__ == "__main__":
    main()
