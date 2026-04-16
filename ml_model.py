import pandas as pd
import numpy as np
import os
import joblib
from sklearn.ensemble import RandomForestClassifier

MODEL_PATH = 'trained_rf_model.pkl'

def train_dummy_model():
    '''
    Trains a mock model and returns feature importances.
    In production, use real historical data with labels (1 for successful breakout, 0 for failed).
    '''
    # Generate mock training data
    np.random.seed(42)
    features = ['volume_ratio', 'candle_strength', 'breakout_strength', 'vwap_distance']
    
    X = pd.DataFrame(np.random.randn(1000, 4), columns=features)
    y = np.random.randint(0, 2, 1000)
    
    model = RandomForestClassifier(n_estimators=50, random_state=42)
    model.fit(X, y)
    
    # Save model
    joblib.dump(model, MODEL_PATH)
    
    # Extract feature importance
    importance = dict(zip(features, model.feature_importances_))
    
    return importance

def predict_success_probability(features_dict: dict) -> float:
    '''
    Predicts probability of success (0-1).
    '''
    if not os.path.exists(MODEL_PATH):
        # Fallback if model not trained
        train_dummy_model()
        
    model = joblib.load(MODEL_PATH)
    
    # Extract required features and shape for prediction
    req_feats = ['volume_ratio', 'candle_strength', 'breakout_strength', 'vwap_distance']
    
    # Use 0 as default if missing (in case features aren't calculating correctly)
    data = [features_dict.get(f, 0.0) for f in req_feats]
    
    # Replace NaNs with 0
    data = [0.0 if pd.isna(x) else x for x in data]
    
    X_input = pd.DataFrame([data], columns=req_feats)
    
    # Model predict_proba returns array of probabilities for [class_0, class_1]
    proba = model.predict_proba(X_input)[0][1]
    
    return float(proba)

if __name__ == '__main__':
    importances = train_dummy_model()
    print("Model Trained! Feature Importances:")
    print(importances)
