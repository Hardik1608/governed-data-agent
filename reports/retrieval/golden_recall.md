# Catalog retrieval on the golden set

110 answerable questions; 104 catalog documents (models, columns, certified metrics); embeddings: nomic-embed-text. A question counts as a hit at k when every model its gold SQL reads appears in the top-k results (as the model document or one of its columns).

| Retriever | all models in top 3 | all models in top 5 | all models in top 8 |
|---|---|---|---|
| bm25 | 75.4% | 84.5% | 87.3% |
| embedding | 73.6% | 80.0% | 82.7% |
| hybrid | 79.1% | 80.9% | 83.6% |
