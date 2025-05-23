import torch
import torch.nn as nn
import torch.nn.functional as F

from transformers import AutoTokenizer, AutoModelForCausalLM
from transformers import BitsAndBytesConfig


# Class for attention pooling
class AttentionPooling(nn.Module):
    # Generator
    def __init__(self, torch_dtype, hidden_dim):
        super().__init__()
        self.attn_fc = nn.Linear(hidden_dim, 1)
        self.attn_fc.to(dtype=torch_dtype)
        self.ReLU = nn.ReLU()
        self.Tanh = nn.Tanh()
        self.dropout = nn.Dropout(0.1)

    # Forward
    def forward(self, last_hidden_state, attention_mask):
        # Dtype conversion
        attention_mask = attention_mask.to(dtype=last_hidden_state.dtype)

        # Get attention scores
        attn_scores = self.attn_fc(last_hidden_state)
        attn_scores = self.ReLU(attn_scores)
        # attn_scores = self.Tanh(attn_scores)
        attn_scores = self.dropout(attn_scores)

        # Ignore padding tokens
        attention_mask = attention_mask.unsqueeze(-1)
        attn_scores = attn_scores.masked_fill(attention_mask == 0, -1e9)

        # Softmax over sequence
        attn_weights = F.softmax(attn_scores, dim=1)

        # Pooling
        attn_embeddings = (last_hidden_state * attn_weights).sum(dim=1)

        return attn_embeddings


# Function for mean pooling
def mean_pooling(last_hidden_state, attention_mask):
    # Dtype conversion
    attention_mask = attention_mask.to(dtype=last_hidden_state.dtype)

    # Prepare broadcasting
    expanded_attention_mask = attention_mask.unsqueeze(-1).expand_as(last_hidden_state)

    # Zero-out & Sum with given hidden states
    sum_embeddings = (last_hidden_state * expanded_attention_mask).sum(dim=1)

    # Count of non-masked tokens per sample
    sum_mask = expanded_attention_mask.sum(dim=1).clamp(min=1e-9)

    # Mean pooling
    mean_embeddings = sum_embeddings / sum_mask

    return mean_embeddings


# Function to define Huber loss
def compute_huber_loss(preds, labels, delta):
    # Assertion
    assert preds.shape == labels.shape

    # Define the values of abs_error, delta
    delta = torch.tensor(delta, dtype=preds.dtype, device=preds.device)
    abs_error = torch.abs(preds - labels)

    # Flag
    is_small_error = abs_error <= delta

    # Compute & Select loss
    small_error_loss = 0.5 * (abs_error**2)
    large_error_loss = delta * (abs_error - 0.5 * delta)
    loss = torch.where(is_small_error, small_error_loss, large_error_loss)

    return loss.mean()


# Custom model to fine-tune EXAONE on regression task
class LlamaRegressionModel(nn.Module):
    # Generator
    def __init__(self, model, torch_dtype, loss_cat, delta):
        super().__init__()
        self.backbone = model
        hidden_size = self.backbone.config.hidden_size
        self.attention_pooling = AttentionPooling(torch_dtype, hidden_size)
        self.regressor = nn.Sequential(
            nn.Linear(hidden_size * 2, hidden_size),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(hidden_size, 1),
            nn.Sigmoid(),
        ).to(dtype=torch_dtype)
        self.loss_cat = loss_cat
        self.delta = delta

    # Forward with last hidden state's last token
    def forward(self, input_ids=None, attention_mask=None, labels=None, **kwargs):
        input_dicts = {"input_ids": input_ids, "attention_mask": attention_mask}
        outputs = self.backbone(**input_dicts)

        # Pooling (Last token, Mean pooling, ...)
        # pooled = mean_pooling(outputs.last_hidden_state, attention_mask)
        pooled = self.attention_pooling(outputs.last_hidden_state, attention_mask)
        acum_token = outputs.last_hidden_state[:, -1, :]
        total_pooled = torch.cat([pooled, acum_token], dim=1)
        preds = self.regressor(total_pooled).squeeze(-1)

        # Calculate loss
        if self.loss_cat == "mse":
            criterion = nn.MSELoss()
            loss = criterion(preds, labels)
        elif self.loss_cat == "mae":
            criterion = nn.L1Loss()
            loss = criterion(preds, labels)
        else:
            loss = compute_huber_loss(preds, labels, self.delta)

        return {"loss": loss, "logits": preds}

    @property
    def config(self):
        return self.backbone.config


# Function for quantization
def ret_quant_config(torch_dtype):
    quant_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch_dtype,
        bnb_4bit_quant_type="nf4",
        # bnb_4bit_use_double_quant=True,
    )
    return quant_config


# Function to load tokenizer & model
def load_model(model_id, cap_flag, loss_cat, delta):
    # Prepare tokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # Use cap_flag
    if cap_flag:
        torch_dtype = torch.bfloat16
        attn_implementation = "flash_attention_2"
    else:
        torch_dtype = torch.float16
        attn_implementation = "eager"

    # Quantization config
    quant_config = ret_quant_config(torch_dtype=torch_dtype)

    # Load model with upper options
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch_dtype,
        attn_implementation=attn_implementation,
        quantization_config=quant_config,
        device_map={"": 0},
        trust_remote_code=True,
    )

    # Additional configs
    model.config.use_cache = False
    model.config.pretraining_tp = 1
    # model.gradient_checkpointing_enable()

    # Create a child model for specific task
    reg_model = LlamaRegressionModel(model, torch_dtype, loss_cat, delta)

    return tokenizer, reg_model
