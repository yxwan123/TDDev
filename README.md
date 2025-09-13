# Generating Full-Stack Web Applications from Requirements Via Multi-Agent Test-Driven Development
<div align="center">
<a href="https://arxiv.org/abs/2507.22827">
    <img
      src="https://img.shields.io/badge/arXiv-Paper-red?logo=arxiv&logoColor=red"
      alt="Paper on arXiv"
    />
  </a>
  <a href="https://huggingface.co/spaces/Jimmyzheng-10/ScreenCoder">
    <img 
        src="https://img.shields.io/badge/HF-Demo-yellow?logo=huggingface&logoColor=yellow" 
        alt="Huggingface Demo"
    />
  </a>
</div>
<div align="center">
  <img src="tdd_advantage.pnf" width="100%"/>
  
</div>

## Abstract
Developing full-stack web applications is complex and time-intensive, demanding proficiency across diverse technologies and frameworks. Although recent advances in multimodal large language models (MLLMs) enable automated webpage generation from visual inputs, current solutions remain limited to front-end tasks and fail to deliver fully functional applications. In this work, we introduce TDDev, the first test-driven development (TDD)-enabled LLM-agent framework for end-to-end full-stack web application generation. Given a natural language description or design image, TDDev automatically derives executable test cases, generates front-end and back-end code, simulates user interactions, and iteratively refines the implementation until all requirements are satisfied. Our framework addresses key challenges in full-stack automation, including underspecified user requirements, complex interdependencies among multiple files, and the need for both functional correctness and visual fidelity. Through extensive experiments on diverse application scenarios, TDDev achieves a 14.4% improvement on overall accuracy compared to state-of-the-art baselines, demonstrating its effectiveness in producing reliable, high-quality web applications without requiring manual intervention.

**TODO List**
- [ ] Code Implementation 
- [ ] HuggingFace Dataset


## More Projects on MLLM for Web/Code Generation
- [WebPAI (Web Development Powered by AI)](https://github.com/WebPAI) released a set of research resources and datasets for webpage generation studies, aiming to build an AI platform for more reliable and practical automated webpage generation.

- [Awesome-Multimodal-LLM-for-Code](https://github.com/xjywhu/Awesome-Multimodal-LLM-for-Code) maintains a comprehensive list of papers on methods, benchmarks, and evaluation for code generation under multimodal scenarios.


## Acknowledgements

This project builds upon several outstanding open-source efforts. We would like to thank the authors and contributors of the following projects: [Bolt].diy](https://github.com/stackblitz-labs/bolt.diy), [Browser-Use](https://github.com/browser-use/browser-use)


