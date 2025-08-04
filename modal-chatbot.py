import os

import base64

import modal

from fastapi import File, UploadFile, Form

from llama_cpp import Llama

import gradio as gr

from PIL import Image

import io

import fitz # Import PyMuPDF



MODEL_REPO = "openbmb/MiniCPM-V-2_6-gguf"

GGUF_FILENAME = "ggml-model-Q4_K_M.gguf"

MODEL_DIR = "/models/MiniCPM-V_2_6"

MODEL_PATH = f"{MODEL_DIR}/{GGUF_FILENAME}"

DEFAULT_SYSTEM_MESSAGE = "You are Bella, a helpful assistant."

TOKEN_LIMIT = 256

MAX_TEXT_INPUT_LENGTH = 2000 # Example: Limit text input to 2000 characters

MAX_IMAGE_FILE_SIZE_MB = "10MB" # Example: Limit image file size to 10 MB



app = modal.App("bella-minicpm-v2")



# ✅ Create or attach named volume

volume = modal.Volume.from_name("minicpm-model", create_if_missing=True)



image = (

    modal.Image.debian_slim()

    .apt_install("build-essential", "cmake", "git", "libgl1") # Added libgl1 for Pillow

    .pip_install(

        "llama-cpp-python>=0.3.9",

        "gradio",

        "fastapi",

        "uvicorn",

        "huggingface_hub",

        "Pillow",

        "PyMuPDF"

    )

)



@app.function(

    image=image,

    volumes={"/models": volume},

    timeout=900

)

def download_model():

    from huggingface_hub import hf_hub_download

    print("Downloading model...")

    hf_hub_download(

        repo_id=MODEL_REPO,

        filename=GGUF_FILENAME,

        local_dir=MODEL_DIR,

        local_dir_use_symlinks=False

    )

    volume.commit()

    print("Download complete and committed.")



@app.function(

    image=image,

    volumes={"/models": volume},

    gpu="L4",

    timeout=3600,

    min_containers=0

)

@modal.fastapi_endpoint(method="POST")

def ask_web(

    q: str = Form(""),

    system: str = Form(DEFAULT_SYSTEM_MESSAGE),

    tokens: int = Form(TOKEN_LIMIT),

    image_file: UploadFile = File(None),

    image_base64: str = Form(None)

):

    llm = Llama(

        model_path=MODEL_PATH,

        n_ctx=4096,

        n_gpu_layers=100,

        n_threads=os.cpu_count()

    )

    messages = [{"role": "system", "content": system}]



    img_bytes = None

    if image_file:

        img_bytes = image_file.file.read()

    elif image_base64:

        img_bytes = base64.b64decode(image_base64)



    if img_bytes:

        image_tag = f"<image>{base64.b64encode(img_bytes).decode()}</image>"

        q += "\n" + image_tag



    messages.append({"role": "user", "content": q})

    try:

        resp = llm.create_chat_completion(

            messages=messages,

            max_tokens=tokens,

            temperature=0.7,

            top_p=0.9,

            repeat_penalty=1.1,

            stop=["<|im_end|>", "</s>", "<|end_of_text|>"]

        )

        return {"answer": resp["choices"][0]["message"]["content"]}

    except Exception as e:

        return {"error": str(e)}



@app.function(

    image=image,

    volumes={"/models": volume},

    gpu="L4", # Confirm GPU utilization as discussed

    timeout=3600,

    min_containers=0

)

def serve():

    llm = Llama(

        model_path=MODEL_PATH,

        n_ctx=4096,

        n_gpu_layers=100, # Adjust based on your GPU setup and llama_cpp_python build

        n_threads=os.cpu_count()

    )



    def llm_query(messages, max_tokens, image_data=None):

        stop_tokens = ["<|im_end|>", "</s>", "<|end_of_text|>"]

        full = ""

       

        chat_completion_args = {

            "messages": messages,

            "stream": True,

            "max_tokens": max_tokens,

            "temperature": 0.7,

            "top_p": 0.9,

            "repeat_penalty": 1.1,

            "stop": stop_tokens

        }



        if image_data:

            if isinstance(image_data, Image.Image):

                buffered = io.BytesIO()

                image_data.save(buffered, format="PNG")

                image_bytes = buffered.getvalue()

            elif isinstance(image_data, bytes):

                image_bytes = image_data

            else:

                print(f"Warning: Unexpected image_data type: {type(image_data)}")

                image_bytes = None



            if image_bytes:

                # Base64 encode the image bytes

                base64_image = base64.b64encode(image_bytes).decode('utf-8')

               

                # Add the image part to the user_content list

                messages.append({"role": "user", "content": image_bytes})

                #{"role":"user", "content":[{"type":"text","text":"Describe this image:"}, image_msg]



                """messages.append({

                    "type": "image_url",

                    "image_url": {

                        "url": f"data:image/png;base64,{base64_image}"

                    }

                })"""

                #chat_completion_args["images"] = [image_bytes]



        for chunk in llm.create_chat_completion(**chat_completion_args):

            full += chunk["choices"][0]["delta"].get("content", "")

            yield full



    # --- Modified chat_fn with safety rails and alerts ---

    def chat_fn(message, history, max_tokens, system, image_input, pdf_input):

        new_history = history if history is not None else []

       

        # --- Safety Rail 1: Empty Input Check ---

        if not message and not image_input and not pdf_input:

            gr.Warning("Please enter a message, upload an image, or upload a PDF.")

            yield new_history, gr.update(value="", interactive=True)

            return



        # --- Safety Rail 2: Text Input Length Check ---

        if message and len(message) > MAX_TEXT_INPUT_LENGTH:

            gr.Warning(f"Your message is too long ({len(message)} chars). Please shorten it to under {MAX_TEXT_INPUT_LENGTH} characters.")

            yield new_history, gr.update(value="", interactive=True)

            return



        user_message_content = message

        image_data_to_llm = None

       

        if image_input:

            # --- Safety Rail 3: Image File Size Check (Gradio's built-in max_file_size is better for this) ---

            # You'd typically set this on the gr.Image component directly:

            # gr.Image(type="pil", label="Upload Image", sources=["upload"], interactive=True, type="filepath", file_count="single", live=False, file_types=["image"], max_file_size=MAX_IMAGE_FILE_SIZE_MB * 1024 * 1024)

            # If you still want a Python-side check:

            # if image_input.size > MAX_IMAGE_FILE_SIZE_MB * 1024 * 1024:

            #     gr.Warning(f"Image is too large. Max allowed is {MAX_IMAGE_FILE_SIZE_MB} MB.")

            #     yield new_history, gr.update(value="", interactive=True)

            #     return



            image_data_to_llm = image_input

            user_message_content = "<image>\n" + message

        elif pdf_input:

            try:

                # --- Safety Rail 4: PDF File Size Check (can be done with Gradio's gr.File directly) ---

                # gr.File(label="Upload PDF", type="filepath", file_types=[".pdf"], interactive=True, max_file_size=MAX_IMAGE_FILE_SIZE_MB * 1024 * 1024)

                # If doing it here, you'd need the file path and check its size before opening.

               

                doc = fitz.open(pdf_input.name)

                # For simplicity, we are still only processing the first page as an image

                # For multiple pages, you'd iterate and potentially pass multiple images or perform RAG.

                page = doc.load_page(0)

                pix = page.get_pixmap()

                pdf_image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                image_data_to_llm = pdf_image

                doc.close()

                user_message_content = "<image>\n" + message + "\n(Content from PDF page 1)"

                gr.Info("Processing PDF...") # Informative message

                print(f"Processed PDF: {pdf_input.name}, taking first page as image.")

            except Exception as e:

                gr.Error(f"Error processing PDF: {e}. Please try another file.")

                print(f"Error processing PDF: {e}")

                yield new_history, gr.update(value="", interactive=True)

                return



        new_history.append({"role": "user", "content": user_message_content})

        yield new_history, gr.update(value="", interactive=False)



        messages = [{"role": "system", "content": system}] + new_history

        current_response_content = ""

       

        try:

            for chunk in llm_query(messages, max_tokens, image_data=image_data_to_llm):

                current_response_content = chunk

                if new_history and new_history[-1]["role"] == "assistant":

                    new_history[-1]["content"] = current_response_content

                else:

                    new_history.append({"role": "assistant", "content": current_response_content})

                yield new_history, gr.update(value="", interactive=False)

        except Exception as e:

            gr.Error(f"An error occurred during response generation: {e}. Please try again.")

            print(f"LLM generation error: {e}")

            yield new_history, gr.update(value="", interactive=True)

            return



        yield new_history, gr.update(interactive=True)



    with gr.Blocks() as demo:

        gr.Markdown("# 🤖 Bella (MiniCPM-V-2.6 GGUF)")

        with gr.Row():

            with gr.Column(scale=4):

                chatbot = gr.Chatbot(height=500, type="messages")

                msg = gr.Textbox(placeholder="Ask Bella a question...", show_label=False)

                with gr.Row():

                    submit_btn = gr.Button("Send")

                    clear_btn = gr.Button("Clear")

            with gr.Column(scale=1):

                # Using max_file_size directly in Gradio component for client-side check

                image_input = gr.Image(

                    type="pil",

                    label="Upload Image (coming soon)",

                    interactive=False,

                    elem_id="image_disabled"

                )

                pdf_input = gr.File(

                    label="Upload PDF (coming soon)",

                    type="filepath",

                    file_types=[".pdf"],

                    interactive=False,

                    elem_id="pdf_disabled"

                )



                token_slider = gr.Slider(64, 1024, value=256, step=16, label="Max tokens")

                system_box = gr.Textbox(value=DEFAULT_SYSTEM_MESSAGE, label="System Prompt")

                gr.HTML("""

                <style>

                #image_disabled, #pdf_disabled {

                    position: relative;

                    pointer-events: auto; /* Allow hover even if disabled */

                    cursor: not-allowed;

                }

                #image_disabled::after, #pdf_disabled::after {

                    content: "Coming soon when MiniCPM is fully integrated without quantization.";

                    position: absolute;

                    bottom: 100%;

                    left: 0;

                    width: 220px;

                    background-color: #333;

                    color: #fff;

                    text-align: center;

                    padding: 6px;

                    border-radius: 4px;

                    font-size: 12px;

                    visibility: hidden;

                    opacity: 0;

                    transition: opacity 0.3s;

                    z-index: 100;

                }

                #image_disabled:hover::after, #pdf_disabled:hover::after {

                    visibility: visible;

                    opacity: 1;

                }

                </style>

                """)





        submit_btn.click(chat_fn, [msg, chatbot, token_slider, system_box, image_input, pdf_input], [chatbot, msg], queue=True)

        msg.submit(chat_fn, [msg, chatbot, token_slider, system_box, image_input, pdf_input], [chatbot, msg], queue=True)

        clear_btn.click(lambda: ([], "", None, None), outputs=[chatbot, msg, image_input, pdf_input], queue=False)

   

    # demo.queue().launch(server_name="0.0.0.0", server_port=7860, auth=("bawn", "password"), max_file_size="5MB")

    demo.queue().launch(server_name="0.0.0.0", server_port=7860,max_file_size="5MB", share="True")



if __name__ == "__main__":

    download_model.remote()

    serve.remote()